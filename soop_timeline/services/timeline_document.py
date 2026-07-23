from __future__ import annotations


AI_TIMELINE_NOTICE_LINES = (
    "본 타임라인은 AI로 작성되어 수동 작성본보다 정확도가 낮습니다.",
    "직접 작성을 원하시는 분이 계신다면 언제든 자리 양보하겠습니다.",
)
AI_TIMELINE_NOTICE = "\n".join(AI_TIMELINE_NOTICE_LINES)
_LEGACY_AI_TIMELINE_NOTICE_LINES = (
    (
        "본 타임라인은 AI로 작성되어 수동 작성본보다 정확도가 낮습니다.",
        "직접 작성을 원하시는 분이 계신다면 언제든 자리를 양보하겠습니다.",
    ),
)


def _starts_with_lines(document: str, expected: tuple[str, ...]) -> bool:
    lines = document.splitlines()
    return tuple(lines[: len(expected)]) == expected


def has_ai_timeline_notice(document: str) -> bool:
    source = str(document or "").lstrip("\ufeff")
    return _starts_with_lines(source, AI_TIMELINE_NOTICE_LINES)


def ensure_ai_timeline_notice(document: str) -> str:
    """Put the fixed AI disclosure at the very top without duplicating it."""
    source = str(document or "")
    if has_ai_timeline_notice(source):
        return source
    normalized = source.lstrip("\ufeff")
    for legacy_lines in _LEGACY_AI_TIMELINE_NOTICE_LINES:
        if not _starts_with_lines(normalized, legacy_lines):
            continue
        source_lines = normalized.splitlines(keepends=True)
        remainder = "".join(source_lines[len(legacy_lines) :])
        return f"{AI_TIMELINE_NOTICE}\n{remainder}"
    content = normalized.lstrip("\r\n")
    return f"{AI_TIMELINE_NOTICE}\n\n{content}"


def initial_timeline_document(content_title: str) -> str:
    return ensure_ai_timeline_notice(
        f"오늘의 콘텐츠: {str(content_title).strip()}\n\n"
    )
