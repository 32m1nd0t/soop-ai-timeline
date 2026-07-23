from __future__ import annotations


AI_TIMELINE_NOTICE_LINES = (
    "본 타임라인은 AI로 작성되어 수동 작성본보다 정확도가 낮습니다.",
    "직접 작성을 원하시는 분이 계신다면 언제든 자리를 양보하겠습니다.",
)
AI_TIMELINE_NOTICE = "\n".join(AI_TIMELINE_NOTICE_LINES)


def has_ai_timeline_notice(document: str) -> bool:
    lines = str(document or "").lstrip("\ufeff").splitlines()
    return tuple(lines[: len(AI_TIMELINE_NOTICE_LINES)]) == AI_TIMELINE_NOTICE_LINES


def ensure_ai_timeline_notice(document: str) -> str:
    """Put the fixed AI disclosure at the very top without duplicating it."""
    source = str(document or "")
    if has_ai_timeline_notice(source):
        return source
    content = source.lstrip("\ufeff\r\n")
    return f"{AI_TIMELINE_NOTICE}\n\n{content}"


def initial_timeline_document(content_title: str) -> str:
    return ensure_ai_timeline_notice(
        f"오늘의 콘텐츠: {str(content_title).strip()}\n\n"
    )
