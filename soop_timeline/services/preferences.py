from __future__ import annotations

from dataclasses import dataclass


LIVE_AI_MODE_SETTING = "live_ai_mode"
DISCOVERY_INTERVAL_SETTING = "discovery_interval_minutes"
NEW_VOD_NOTIFICATION_SETTING = "new_vod_notification"
CACHE_RETENTION_SETTING = "transcript_cache_retention_days"
PRIVACY_NOTICE_SETTING = "privacy_notice_version"
PRIVACY_NOTICE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class LiveAIMode:
    mode_id: str
    label: str
    first_summary_seconds: int
    interval_seconds: int

    @property
    def estimated_calls_per_hour(self) -> int:
        return max(1, round(3600 / self.interval_seconds))


LIVE_AI_MODES: dict[str, LiveAIMode] = {
    "saving": LiveAIMode(
        "saving",
        "절약 · 약 15분마다 정리",
        5 * 60,
        15 * 60,
    ),
    "balanced": LiveAIMode(
        "balanced",
        "기본 · 약 8분마다 정리",
        2 * 60,
        8 * 60,
    ),
    "frequent": LiveAIMode(
        "frequent",
        "자주 갱신 · 약 3분마다 정리",
        60,
        3 * 60,
    ),
}


def normalize_live_ai_mode(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in LIVE_AI_MODES else "saving"


def live_ai_mode(value: str) -> LiveAIMode:
    return LIVE_AI_MODES[normalize_live_ai_mode(value)]


def estimated_live_calls(duration_seconds: float, mode: str) -> int:
    spec = live_ai_mode(mode)
    duration = max(0.0, float(duration_seconds or 0.0))
    if duration <= 0:
        return spec.estimated_calls_per_hour + 1
    summaries = max(
        1,
        1 + int(max(0.0, duration - spec.first_summary_seconds) // spec.interval_seconds),
    )
    return summaries + 1


def normalized_discovery_interval(value: str | int) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 180
    return minutes if minutes in {0, 30, 60, 180, 360} else 180


def normalized_cache_retention(value: str | int) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 0
    return days if days in {0, 30, 90, 180} else 0


def setting_enabled(value: str, *, default: bool = True) -> bool:
    normalized = str(value or ("1" if default else "0")).strip().lower()
    return normalized not in {"0", "false", "no", "off"}
