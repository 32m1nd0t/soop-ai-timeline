from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable


Clock = Callable[[], float]


@dataclass(slots=True)
class EtaEstimator:
    """Estimate remaining wall time from completed work units."""

    total_units: float
    clock: Clock = time.monotonic
    _started_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.total_units = max(0.0, float(self.total_units))
        self._started_at = self.clock()

    def remaining_seconds(self, completed_units: float) -> float | None:
        completed = max(0.0, min(self.total_units, float(completed_units)))
        elapsed = max(0.0, self.clock() - self._started_at)
        if self.total_units <= 0 or completed <= 0 or elapsed < 0.25:
            return None
        remaining_units = self.total_units - completed
        if remaining_units <= 0:
            return 0.0
        units_per_second = completed / elapsed
        if units_per_second <= 0:
            return None
        return remaining_units / units_per_second


def humanize_duration(seconds: float) -> str:
    value = max(0.0, float(seconds))
    if value < 60:
        return "1분 이내"

    total_minutes = max(1, math.ceil(value / 60.0))
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}분"
    if hours < 24:
        return f"{hours}시간" + (f" {minutes}분" if minutes else "")

    days, remaining_hours = divmod(hours, 24)
    return f"{days}일" + (f" {remaining_hours}시간" if remaining_hours else "")


def format_eta(remaining_seconds: float | None, now: datetime | None = None) -> str:
    if remaining_seconds is None:
        return "예상 시간 계산 중…"

    current = now or datetime.now()
    remaining = max(0.0, float(remaining_seconds))
    completion = current + timedelta(seconds=remaining)
    if completion.date() == current.date():
        completion_label = completion.strftime("%H:%M")
    elif completion.date() == (current + timedelta(days=1)).date():
        completion_label = completion.strftime("내일 %H:%M")
    else:
        completion_label = completion.strftime("%m/%d %H:%M")
    return f"남은 시간 약 {humanize_duration(remaining)} · 예상 완료 {completion_label}"
