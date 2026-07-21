from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VodState(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    ANALYZING = "analyzing"
    REVIEW = "review"
    READY = "ready"
    COPIED = "copied"
    PUBLISHED = "published"
    SKIPPED = "skipped"
    FAILED = "failed"


STATE_LABELS: dict[str, str] = {
    VodState.NEW.value: "신규",
    VodState.QUEUED.value: "분석 대기",
    VodState.ANALYZING.value: "분석 중",
    VodState.REVIEW.value: "검수 중",
    VodState.READY.value: "검수 완료",
    VodState.COPIED.value: "복사 완료",
    VodState.PUBLISHED.value: "등록 완료",
    VodState.SKIPPED.value: "건너뜀",
    VodState.FAILED.value: "오류",
}


@dataclass(slots=True)
class Streamer:
    id: int
    channel_id: str
    display_name: str
    enabled: bool
    added_at: str
    last_checked_at: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class Vod:
    vod_id: str
    streamer_id: int
    channel_id: str
    streamer_name: str
    title: str
    url: str
    duration_text: str
    published_text: str
    thumbnail_url: str
    state: str
    discovered_at: str
    updated_at: str
    source_kind: str = "vod"


@dataclass(slots=True)
class TimelineDocument:
    vod_id: str
    text: str
    status: str
    updated_at: str


@dataclass(slots=True, frozen=True)
class TimelineRevision:
    id: int
    vod_id: str
    text: str
    reason: str
    created_at: str
