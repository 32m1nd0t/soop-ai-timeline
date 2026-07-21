from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import threading
import time
from typing import Callable

from .transcription import AnalysisCancelled


GEMINI_PROVIDER = "gemini"
OPENAI_PROVIDER = "openai"
ANTHROPIC_PROVIDER = "anthropic"
DEFAULT_AI_PROVIDER = GEMINI_PROVIDER


@dataclass(frozen=True, slots=True)
class AIProviderSpec:
    provider_id: str
    display_name: str
    default_model: str
    key_placeholder: str
    environment_variable: str
    package_name: str


AI_PROVIDER_SPECS: dict[str, AIProviderSpec] = {
    GEMINI_PROVIDER: AIProviderSpec(
        GEMINI_PROVIDER,
        "Google Gemini",
        "gemini-3.5-flash",
        "Google AI Studio에서 발급한 Gemini API 키",
        "GEMINI_API_KEY",
        "google.genai",
    ),
    OPENAI_PROVIDER: AIProviderSpec(
        OPENAI_PROVIDER,
        "OpenAI",
        "gpt-5.6-luna",
        "OpenAI API 대시보드에서 발급한 API 키",
        "OPENAI_API_KEY",
        "openai",
    ),
    ANTHROPIC_PROVIDER: AIProviderSpec(
        ANTHROPIC_PROVIDER,
        "Anthropic Claude",
        "claude-sonnet-4-6",
        "Anthropic Console에서 발급한 API 키",
        "ANTHROPIC_API_KEY",
        "anthropic",
    ),
}


def normalize_ai_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    return provider if provider in AI_PROVIDER_SPECS else DEFAULT_AI_PROVIDER


def provider_spec(provider: str) -> AIProviderSpec:
    return AI_PROVIDER_SPECS[normalize_ai_provider(provider)]


def provider_model_setting(provider: str) -> str:
    return f"ai_model_{normalize_ai_provider(provider)}"


@dataclass(slots=True)
class AIUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.calls += 1
        self.input_tokens += max(0, int(input_tokens or 0))
        self.output_tokens += max(0, int(output_tokens or 0))

    def summary(self, provider: str) -> str:
        name = provider_spec(provider).display_name
        if self.input_tokens or self.output_tokens:
            return (
                f"{name} {self.calls:,}회 · 입력 {self.input_tokens:,}토큰 · "
                f"출력 {self.output_tokens:,}토큰"
            )
        return f"{name} {self.calls:,}회"


@dataclass(frozen=True, slots=True)
class StructuredAIResponse:
    payload: dict[str, object]
    input_tokens: int = 0
    output_tokens: int = 0


CancelCallback = Callable[[], bool]
_validated_connections: set[str] = set()
_validation_lock = threading.Lock()


class StructuredAIProvider(ABC):
    def __init__(self, api_key: str, model_name: str):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip()
        self.usage = AIUsage()

    @property
    @abstractmethod
    def provider_id(self) -> str:
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        return provider_spec(self.provider_id).display_name

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    @property
    def unavailable_reason(self) -> str:
        if not self.api_key:
            return f"설정에서 {self.display_name} API 키를 입력하세요."
        package = provider_spec(self.provider_id).package_name
        try:
            package_available = importlib.util.find_spec(package) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            package_available = False
        if not package_available:
            return f"{package} API 모듈이 설치되지 않았습니다."
        if not self.model_name:
            return f"{self.display_name} 모델 이름을 입력하세요."
        return ""

    def request_json(
        self,
        prompt: str,
        schema: dict[str, object],
        cancelled: CancelCallback,
        *,
        purpose: str = "timeline",
    ) -> dict[str, object]:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        last_error: Exception | None = None
        for attempt in range(3):
            if cancelled():
                raise AnalysisCancelled("AI 요청을 취소했습니다.")
            try:
                response = self._perform_request(
                    prompt,
                    strict_json_schema(schema),
                    purpose=purpose,
                )
                if not isinstance(response.payload, dict):
                    raise RuntimeError("AI 응답이 JSON 객체가 아닙니다.")
                self.usage.add(response.input_tokens, response.output_tokens)
                return response.payload
            except AnalysisCancelled:
                raise
            except Exception as error:
                last_error = error
                if attempt < 2:
                    _interruptible_backoff(2**attempt, cancelled)
        raise RuntimeError(
            f"{self.display_name} 요청에 실패했습니다: {_safe_error(last_error)}"
        ) from last_error

    def test_connection(
        self,
        cancelled: CancelCallback | None = None,
        *,
        force: bool = False,
    ) -> str:
        is_cancelled = cancelled or (lambda: False)
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        signature = self._connection_signature()
        with _validation_lock:
            if not force and signature in _validated_connections:
                return f"{self.display_name} 연결 확인됨 · {self.model_name}"
        schema = {
            "type": "object",
            "properties": {"status": {"type": "string", "enum": ["ok"]}},
            "required": ["status"],
            "additionalProperties": False,
        }
        payload = self.request_json(
            "연결 확인입니다. status를 정확히 ok로 반환하세요.",
            schema,
            is_cancelled,
            purpose="connection_test",
        )
        if str(payload.get("status", "")).lower() != "ok":
            raise RuntimeError("연결 응답을 확인하지 못했습니다.")
        with _validation_lock:
            _validated_connections.add(signature)
        return f"{self.display_name} 연결 성공 · {self.model_name}"

    def _connection_signature(self) -> str:
        digest = hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()[:16]
        return f"{self.provider_id}:{self.model_name}:{digest}"

    @abstractmethod
    def _perform_request(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        purpose: str,
    ) -> StructuredAIResponse:
        raise NotImplementedError


class GeminiProvider(StructuredAIProvider):
    @property
    def provider_id(self) -> str:
        return GEMINI_PROVIDER

    def _perform_request(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        purpose: str,
    ) -> StructuredAIResponse:
        del purpose
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        config = types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=schema,
        )
        response = client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config,
        )
        text = str(getattr(response, "text", "") or "").strip()
        payload = _parse_json_text(text, self.display_name)
        usage = getattr(response, "usage_metadata", None)
        return StructuredAIResponse(
            payload,
            _int_attr(usage, "prompt_token_count"),
            _int_attr(usage, "candidates_token_count"),
        )


class OpenAIProvider(StructuredAIProvider):
    @property
    def provider_id(self) -> str:
        return OPENAI_PROVIDER

    def _perform_request(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        purpose: str,
    ) -> StructuredAIResponse:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        response = client.responses.create(
            model=self.model_name,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": _schema_name(purpose),
                    "schema": schema,
                    "strict": True,
                }
            },
            store=False,
        )
        text = str(getattr(response, "output_text", "") or "").strip()
        payload = _parse_json_text(text, self.display_name)
        usage = getattr(response, "usage", None)
        return StructuredAIResponse(
            payload,
            _int_attr(usage, "input_tokens"),
            _int_attr(usage, "output_tokens"),
        )


class AnthropicProvider(StructuredAIProvider):
    @property
    def provider_id(self) -> str:
        return ANTHROPIC_PROVIDER

    def _perform_request(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        purpose: str,
    ) -> StructuredAIResponse:
        del purpose
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        response = client.messages.create(
            model=self.model_name,
            max_tokens=16_384,
            messages=[{"role": "user", "content": prompt}],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": schema,
                }
            },
        )
        blocks = getattr(response, "content", []) or []
        text = "".join(
            str(getattr(block, "text", "") or "")
            for block in blocks
            if getattr(block, "type", "text") == "text"
        ).strip()
        payload = _parse_json_text(text, self.display_name)
        usage = getattr(response, "usage", None)
        return StructuredAIResponse(
            payload,
            _int_attr(usage, "input_tokens"),
            _int_attr(usage, "output_tokens"),
        )


def create_ai_provider(
    provider: str,
    api_key: str,
    model_name: str = "",
) -> StructuredAIProvider:
    normalized = normalize_ai_provider(provider)
    model = model_name.strip() or provider_spec(normalized).default_model
    if normalized == OPENAI_PROVIDER:
        return OpenAIProvider(api_key, model)
    if normalized == ANTHROPIC_PROVIDER:
        return AnthropicProvider(api_key, model)
    return GeminiProvider(api_key, model)


def strict_json_schema(schema: dict[str, object]) -> dict[str, object]:
    """Return a cross-provider schema accepted by strict JSON modes."""
    result = deepcopy(schema)

    def visit(node: object) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            node.setdefault("additionalProperties", False)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for child in properties.values():
                visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for keyword in ("anyOf", "oneOf", "allOf"):
            variants = node.get(keyword)
            if isinstance(variants, list):
                for child in variants:
                    visit(child)

    visit(result)
    return result


def estimate_timeline_calls(duration_seconds: float) -> int:
    # 45-minute windows overlap by two minutes, followed by one final pass.
    duration = max(0.0, float(duration_seconds or 0.0))
    if duration <= 0:
        return 2
    window = 45 * 60
    advance = window - 2 * 60
    windows = 1 if duration <= window else 1 + int((duration - window + advance - 1) // advance)
    return windows + 1


def _parse_json_text(text: str, provider_name: str) -> dict[str, object]:
    if not text:
        raise RuntimeError(f"{provider_name}가 빈 응답을 반환했습니다.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{provider_name} JSON 응답을 해석하지 못했습니다.") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider_name} 응답 형식이 올바르지 않습니다.")
    return payload


def _int_attr(value: object, name: str) -> int:
    try:
        return max(0, int(getattr(value, name, 0) or 0))
    except (TypeError, ValueError):
        return 0


def _schema_name(value: str) -> str:
    cleaned = "".join(character if character.isalnum() else "_" for character in value)
    return (cleaned.strip("_") or "soop_timeline")[:64]


def _safe_error(error: Exception | None) -> str:
    if error is None:
        return "알 수 없는 오류"
    return " ".join(str(error).split())[:800]


def _interruptible_backoff(seconds: float, cancelled: CancelCallback) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancelled():
            raise AnalysisCancelled("AI 요청을 취소했습니다.")
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
