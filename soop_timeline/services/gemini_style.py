from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .ai_provider import (
    GEMINI_PROVIDER,
    StructuredAIProvider,
    create_ai_provider,
)
from .credentials import get_gemini_api_key
from .transcription import AnalysisCancelled


DEFAULT_GEMINI_STYLE_MODEL = "gemini-flash-lite-latest"
TITLE_PATTERN = re.compile(r"^\s*오늘의\s*콘텐츠\s*:\s*(?P<title>.*)$")
ENTRY_PATTERN = re.compile(
    r"^(?P<timestamp>\d{1,3}:[0-5]\d:[0-5]\d)\s+(?P<summary>\S.*)$"
)
DIRECT_QUOTE_PATTERN = re.compile(r'"[^"\n]+"')


DRY_TIMELINE_STYLE_GUIDE = """
문체 규칙:
- 설명문이 아니라 타임라인 소제목처럼 간결하고 자연스러운 제목형·메모체로 작성합니다.
- `합니다`, `입니다`, `됩니다`, `했습니다`, `있습니다`, `하세요` 같은 존댓말 종결어미와 마침표를 사용하지 않습니다.
- 과장·상투적인 수식어는 줄이되, 실제 인물·사건·질문·게임 상황은 구체적이고 생생하게 남깁니다.
- 문맥상 분명한 `오늘 방송은`, `스트리머가`, `방송에서` 같은 상투적인 주어와 상황 설명은 생략합니다.
- 모든 줄을 억지로 같은 틀(`~함`, `~하는 누구` 등)로 만들지 말고, 내용에 맞는 자연스럽고 짧은 표현을 씁니다.
- `~일화`, `~경험`, `~과정`, `~장면`, `~모습`, `~에 따른`, `~을 겪은` 같은 군더더기 틀을 빼고 핵심만 짧게 남깁니다. 짧게 쓸 수 있으면 짧게 씁니다.
- 말투만 다듬으며, 고유명사와 구체적인 사건 정보는 삭제하거나 새로 만들지 않습니다.

문체 예시:
- 나쁜 예: `여름 여행의 불쾌함과 겨울 여행의 낭만에 대해 비교하며 이야기합니다.`
- 좋은 예: `여름·겨울 여행 환경과 선호 비교`
- 나쁜 예: `마이곰이가 보내준 귀여운 그림 선물을 자랑합니다.`
- 좋은 예: `마이곰이의 그림 선물 공개`
- 나쁜 예: `완벽한 타이밍과 각도를 맞추며 고난도 회전 장애물 구간을 돌파하기 위해 고군분투합니다.`
- 좋은 예: `회전 장애물 구간 타이밍 공략`
- 나쁜 예: `오랜만에 MBTI 검사를 다시 진행하며 질문에 답변하기 시작합니다.`
- 좋은 예: `MBTI 검사 재진행`
- 나쁜 예: `화장실 청소용 칫솔로 양치하는 꿈 일화`
- 좋은 예: `화장실 청소용 칫솔로 양치하는 꿈`
- 나쁜 예: `화장실에서 겪은 갑작스러운 쾌변 경험`
- 좋은 예: `갑작스러운 쾌변`
- 나쁜 예: `범파 노래 요청에 따른 즉석 라이브`
- 좋은 예: `범파 노래 부르기`
""".strip()


STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "content_title": {
            "type": "string",
            "description": "건조하고 중립적인 짧은 콘텐츠 제목",
        },
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line_id": {"type": "string"},
                    "summary": {
                        "type": "string",
                        "description": "존댓말 종결어미가 없는 건조한 제목형 요약",
                    },
                },
                "required": ["line_id", "summary"],
            },
        },
    },
    "required": ["content_title", "entries"],
}


@dataclass(frozen=True, slots=True)
class StyleEntry:
    line_id: str
    line_index: int
    timestamp: str
    summary: str


@dataclass(slots=True)
class ParsedTimelineDocument:
    lines: list[str]
    entries: list[StyleEntry]
    content_title: str
    title_line_index: int | None
    trailing_newline: bool

    def rebuild(self, payload: dict[str, object]) -> str:
        expected_ids = {entry.line_id for entry in self.entries}
        replacements: dict[str, str] = {}
        raw_entries = payload.get("entries", [])
        if not isinstance(raw_entries, list):
            raise RuntimeError("AI 문체 교정 응답에 항목 목록이 없습니다.")

        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            line_id = str(raw.get("line_id", "")).strip()
            summary = normalize_summary(str(raw.get("summary", "")))
            if line_id not in expected_ids or not summary or line_id in replacements:
                continue
            replacements[line_id] = summary

        missing = expected_ids - replacements.keys()
        if missing:
            raise RuntimeError(
                f"AI가 타임라인 {len(missing)}개 항목을 누락해 원문을 유지했습니다."
            )

        result = list(self.lines)
        for entry in self.entries:
            replacement = preserve_direct_quotes(
                entry.summary,
                replacements[entry.line_id],
            )
            result[entry.line_index] = (
                f"{entry.timestamp} {replacement}"
            )

        title = normalize_title(str(payload.get("content_title", "")))
        if self.title_line_index is not None and title:
            result[self.title_line_index] = f"오늘의 콘텐츠: {title}"

        document = "\n".join(result)
        if self.trailing_newline:
            document += "\n"
        return document


def parse_timeline_document(document: str) -> ParsedTimelineDocument:
    lines = document.splitlines()
    entries: list[StyleEntry] = []
    content_title = ""
    title_line_index: int | None = None

    for index, line in enumerate(lines):
        if title_line_index is None:
            title_match = TITLE_PATTERN.match(line)
            if title_match is not None:
                title_line_index = index
                content_title = title_match.group("title").strip()
                continue

        entry_match = ENTRY_PATTERN.match(line)
        if entry_match is None:
            continue
        entries.append(
            StyleEntry(
                line_id=f"line_{len(entries):04d}",
                line_index=index,
                timestamp=entry_match.group("timestamp"),
                summary=entry_match.group("summary").strip(),
            )
        )

    return ParsedTimelineDocument(
        lines=lines,
        entries=entries,
        content_title=content_title,
        title_line_index=title_line_index,
        trailing_newline=document.endswith("\n"),
    )


def normalize_summary(value: str) -> str:
    summary = " ".join(value.split()).strip()
    summary = re.sub(r"^\d{1,3}:[0-5]\d:[0-5]\d\s+", "", summary)
    return summary.rstrip(" .")


def normalize_title(value: str) -> str:
    title = " ".join(value.split()).strip()
    title = re.sub(r"^오늘의\s*콘텐츠\s*:\s*", "", title)
    return title.rstrip(" .")


def preserve_direct_quotes(original: str, replacement: str) -> str:
    """Prevent a style-only AI response from adding or editing direct quotes."""
    original_quotes = DIRECT_QUOTE_PATTERN.findall(original)
    replacement_quotes = DIRECT_QUOTE_PATTERN.findall(replacement)
    if original.count('"') != replacement.count('"'):
        return original
    if not original_quotes:
        return original if replacement_quotes else replacement
    if len(original_quotes) != len(replacement_quotes):
        return original
    quote_iterator = iter(original_quotes)
    return DIRECT_QUOTE_PATTERN.sub(lambda _: next(quote_iterator), replacement)


def build_style_prompt(parsed: ParsedTimelineDocument) -> str:
    entries = "\n".join(
        f"{entry.line_id} | {entry.timestamp} | {entry.summary}"
        for entry in parsed.entries
    )
    return f"""
아래는 이미 타임스탬프가 확정된 인터넷 방송 타임라인입니다.
내용과 항목 수는 바꾸지 말고 문체만 더 건조하고 간결하게 교정하세요.

현재 콘텐츠 제목: {parsed.content_title}

보존 규칙:
- 모든 line_id를 정확히 한 번씩 반환합니다.
- line_id와 타임스탬프는 수정하지 않습니다.
- 항목을 추가·삭제·병합·분할하거나 순서를 바꾸지 않습니다.
- 원문에 없는 사실과 감정을 추측하지 않습니다.
- 큰따옴표("…")로 감싼 스트리머 직접 인용은 말투·종결어미 그대로 두고 문체를 바꾸지 않습니다. 큰따옴표 밖의 메모만 건조하게 교정합니다.

{DRY_TIMELINE_STYLE_GUIDE}

교정 대상:
{entries}
""".strip()


class AITimelineStyler:
    def __init__(self, provider: StructuredAIProvider):
        self.provider = provider

    @classmethod
    def from_database(cls, database: object) -> "AITimelineStyler":
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
    def api_key(self) -> str:
        return self.provider.api_key

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    @property
    def available(self) -> bool:
        return self.provider.available

    @property
    def unavailable_reason(self) -> str:
        return self.provider.unavailable_reason

    def restyle(
        self,
        document: str,
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        is_cancelled = cancelled or (lambda: False)
        parsed = parse_timeline_document(document)
        if not parsed.entries:
            raise RuntimeError("교정할 타임라인 항목이 없습니다.")
        if is_cancelled():
            raise AnalysisCancelled("문체 교정을 취소했습니다.")

        payload = self._request_json(
            build_style_prompt(parsed),
            is_cancelled,
        )
        if is_cancelled():
            raise AnalysisCancelled("문체 교정을 취소했습니다.")
        return parsed.rebuild(payload)

    def _request_json(
        self,
        prompt: str,
        cancelled: Callable[[], bool],
    ) -> dict[str, object]:
        return self.provider.request_json(
            prompt,
            STYLE_SCHEMA,
            cancelled,
            purpose="timeline_style",
        )

    def usage_summary(self) -> str:
        return self.provider.usage.summary(self.provider.provider_id)


class GeminiTimelineStyler(AITimelineStyler):
    """Gemini-backed timeline style editor."""

    def __init__(self, api_key: str, model_name: str = DEFAULT_GEMINI_STYLE_MODEL):
        super().__init__(create_ai_provider(GEMINI_PROVIDER, api_key, model_name))
