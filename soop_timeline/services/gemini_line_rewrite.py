from __future__ import annotations

from typing import Callable

from .ai_provider import (
    GEMINI_PROVIDER,
    StructuredAIProvider,
    create_ai_provider,
)
from .credentials import get_gemini_api_key
from .gemini_style import (
    DEFAULT_GEMINI_STYLE_MODEL,
    DRY_TIMELINE_STYLE_GUIDE,
    normalize_summary,
)
from .gemini_timeline import find_phrase_start_time
from .timeline_timestamp import LINE_TIMESTAMP_PATTERN, parse_timestamp
from .transcription import AnalysisCancelled, Transcript, format_timestamp

QUOTE_MODE = "quote"
SUMMARY_MODE = "summary"

EXCERPT_CHAR_LIMIT = 3_500
DEFAULT_TOPIC_SPAN_SECONDS = 180.0

LINE_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "변환된 한 줄 텍스트",
        },
    },
    "required": ["text"],
}


def _normalize_chars(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def build_transcript_excerpt(
    transcript: Transcript,
    seconds: int,
    next_seconds: int,
) -> tuple[str, float]:
    """Return the transcript excerpt for one topic plus its upper time bound."""
    lower = max(0.0, float(seconds) - 20.0)
    if next_seconds > seconds:
        upper = float(next_seconds)
    else:
        upper = float(seconds) + DEFAULT_TOPIC_SPAN_SECONDS
    lines: list[str] = []
    total = 0
    for segment in transcript.segments:
        if segment.start < lower:
            continue
        if segment.start > upper:
            break
        line = f"[{format_timestamp(segment.start)}] {segment.text.strip()}"
        total += len(line) + 1
        if total > EXCERPT_CHAR_LIMIT:
            break
        lines.append(line)
    return "\n".join(lines), upper


def build_quote_prompt(timestamp: str, content: str, excerpt: str) -> str:
    return f"""
아래는 인터넷 방송 자막 발췌와, 그 구간을 정리한 타임라인 항목입니다.

타임라인 항목: {timestamp} {content}

이 항목을 스트리머의 실제 발언을 그대로 따온 직접 인용으로 바꿉니다.
- 자막 발췌에서 이 주제를 가장 잘 보여주는 실제 발언 한 문장(10~40자)을 골라 그대로 반환합니다.
- 자막에 있는 문장만 사용하고, 표기를 바꾸거나 여러 문장을 합치지 않습니다.
- 시각 표기([h:mm:ss])는 제외하고 발언 텍스트만, 큰따옴표 없이 반환합니다.

자막 발췌:
{excerpt}
""".strip()


def build_summary_prompt(timestamp: str, content: str, excerpt: str) -> str:
    return f"""
아래는 인터넷 방송 자막 발췌와, 그 구간을 정리한 타임라인 항목입니다.

타임라인 항목: {timestamp} {content}

이 항목을 내용을 요약한 건조한 제목형 문구 한 줄로 바꿉니다.
- 자막 발췌를 참고해 이 구간에서 실제로 일어난 일을 반영합니다.
- 자막에 없는 사실을 추측하지 않습니다.
- 큰따옴표 없이 요약 텍스트만 반환합니다.

{DRY_TIMELINE_STYLE_GUIDE}

자막 발췌:
{excerpt}
""".strip()


class AITimelineLineRewriter:
    """Rewrite one timeline line as a direct quote or a dry summary."""

    def __init__(self, provider: StructuredAIProvider):
        self.provider = provider

    @classmethod
    def from_database(cls, database: object) -> "AITimelineLineRewriter":
        model = database.get_setting(
            "gemini_model",
            DEFAULT_GEMINI_STYLE_MODEL,
        )
        return cls(
            create_ai_provider(
                GEMINI_PROVIDER,
                get_gemini_api_key(),
                model,
            )
        )

    @property
    def available(self) -> bool:
        return self.provider.available

    @property
    def unavailable_reason(self) -> str:
        return self.provider.unavailable_reason

    def usage_summary(self) -> str:
        return self.provider.usage.summary(self.provider.provider_id)

    def rewrite(
        self,
        mode: str,
        line: str,
        next_seconds: int,
        transcript: Transcript,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        is_cancelled = cancelled or (lambda: False)

        match = LINE_TIMESTAMP_PATTERN.match(line)
        if match is None:
            raise RuntimeError("타임스탬프로 시작하는 타임라인 줄이 아닙니다.")
        timestamp_text = match.group("timestamp")
        seconds = parse_timestamp(timestamp_text)
        if seconds is None:
            raise RuntimeError("줄의 타임스탬프를 읽지 못했습니다.")
        content = line[match.end() :].strip()
        if not content:
            raise RuntimeError("변환할 내용이 없는 줄입니다.")

        excerpt, upper = build_transcript_excerpt(transcript, seconds, next_seconds)
        if not excerpt:
            raise RuntimeError("이 구간의 저장 자막을 찾지 못했습니다.")
        if is_cancelled():
            raise AnalysisCancelled("줄 변환을 취소했습니다.")

        if mode == QUOTE_MODE:
            payload = self.provider.request_json(
                build_quote_prompt(timestamp_text, content, excerpt),
                LINE_REWRITE_SCHEMA,
                is_cancelled,
                purpose="timeline_line_quote",
            )
            quote = " ".join(str(payload.get("text", "")).split()).strip()
            quote = quote.strip('"“”').strip()
            if not quote:
                raise RuntimeError("AI가 인용문을 반환하지 않았습니다.")
            if _normalize_chars(quote) not in _normalize_chars(excerpt):
                raise RuntimeError(
                    "AI가 자막에 없는 문장을 반환해 적용하지 않았습니다. 다시 시도해 보세요."
                )
            label = timestamp_text
            if transcript.words:
                window = [
                    word
                    for word in transcript.words
                    if seconds - 45.0 <= word.start <= upper + 45.0
                ]
                matched = find_phrase_start_time(quote, window)
                if matched is not None:
                    label = format_timestamp(matched)
            return f'{label} "{quote}"'

        payload = self.provider.request_json(
            build_summary_prompt(timestamp_text, content, excerpt),
            LINE_REWRITE_SCHEMA,
            is_cancelled,
            purpose="timeline_line_summary",
        )
        summary = normalize_summary(str(payload.get("text", ""))).strip('"“”').strip()
        if not summary:
            raise RuntimeError("AI가 요약을 반환하지 않았습니다.")
        return f"{timestamp_text} {summary}"
