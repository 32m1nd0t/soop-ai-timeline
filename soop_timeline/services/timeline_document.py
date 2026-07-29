from __future__ import annotations


TIMELINE_NOTICE_SETTING = "timeline_notice"

# Default text prepended to the top of every generated timeline. It is no longer
# hardcoded into the document builders: users can edit or clear it in Settings,
# and the value here is only the initial default for a fresh install.
DEFAULT_TIMELINE_NOTICE = (
    "본 타임라인은 AI로 작성되어 수동 작성본보다 정확도가 낮습니다.\n"
    "직접 작성을 원하시는 분이 계신다면 언제든 자리 양보하겠습니다."
)

_active_notice = DEFAULT_TIMELINE_NOTICE


def set_timeline_notice(text: str | None) -> None:
    """Set the notice prepended to newly generated timelines.

    An empty value disables the notice entirely. Loaded from the DB at startup
    and updated whenever the user saves settings.
    """
    global _active_notice
    _active_notice = str(text or "").strip("\r\n")


def timeline_notice() -> str:
    return _active_notice


def prepend_timeline_notice(document: str) -> str:
    """Put the active notice above ``document`` (no-op when the notice is empty)."""
    body = str(document or "").lstrip("﻿").lstrip("\r\n")
    notice = _active_notice.strip("\r\n")
    if not notice:
        return body
    return f"{notice}\n\n{body}"


def initial_timeline_document(content_title: str) -> str:
    return prepend_timeline_notice(f"오늘의 콘텐츠: {str(content_title).strip()}\n\n")
