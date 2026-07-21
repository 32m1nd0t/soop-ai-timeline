from __future__ import annotations

from dataclasses import dataclass
import re

from .timeline_timestamp import TIMESTAMP_PATTERN, parse_timestamp


LINE_TIMESTAMP = re.compile(
    r"^\s*(?P<timestamp>\d{1,3}:[0-5]\d:[0-5]\d)(?:\s+(?P<summary>.*))?$"
)
TIMESTAMP_LIKE = re.compile(r"^\s*\d{1,3}:\d{1,2}:\d{1,2}")


@dataclass(frozen=True, slots=True)
class TimelineIssue:
    line_number: int
    kind: str
    message: str


def validate_timeline(
    document: str,
    *,
    duration_seconds: int | None = None,
    large_gap_seconds: int = 30 * 60,
) -> list[TimelineIssue]:
    issues: list[TimelineIssue] = []
    previous_seconds: int | None = None
    seen: dict[int, int] = {}
    entry_count = 0

    for line_number, line in enumerate(document.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("오늘의 콘텐츠:"):
            continue
        match = LINE_TIMESTAMP.match(line)
        if match is None:
            if TIMESTAMP_LIKE.match(line):
                issues.append(
                    TimelineIssue(
                        line_number,
                        "invalid",
                        "타임스탬프 형식이 올바르지 않음",
                    )
                )
            continue

        seconds = parse_timestamp(match.group("timestamp"))
        if seconds is None:
            continue
        entry_count += 1
        summary = str(match.group("summary") or "").strip()
        if not summary:
            issues.append(
                TimelineIssue(line_number, "empty", "타임스탬프 뒤 요약이 비어 있음")
            )
        if seconds in seen:
            issues.append(
                TimelineIssue(
                    line_number,
                    "duplicate",
                    f"{seen[seconds]}행과 같은 타임스탬프",
                )
            )
        else:
            seen[seconds] = line_number
        if previous_seconds is not None:
            if seconds < previous_seconds:
                issues.append(
                    TimelineIssue(line_number, "order", "앞 항목보다 시간이 빠름")
                )
            elif seconds - previous_seconds > large_gap_seconds:
                gap_minutes = (seconds - previous_seconds) // 60
                issues.append(
                    TimelineIssue(
                        line_number,
                        "gap",
                        f"앞 항목과 약 {gap_minutes}분 간격 · 누락 여부 확인",
                    )
                )
        if duration_seconds is not None and seconds > duration_seconds + 2:
            issues.append(
                TimelineIssue(line_number, "range", "영상 재생시간을 벗어난 타임스탬프")
            )
        previous_seconds = seconds

    if entry_count == 0:
        issues.append(TimelineIssue(0, "missing", "타임라인 항목이 없음"))
    return issues


def parse_duration_text(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = TIMESTAMP_PATTERN.fullmatch(text)
    if match is not None:
        return parse_timestamp(text)
    parts = text.split(":")
    try:
        numbers = [int(part.strip()) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return None


def format_issue_report(issues: list[TimelineIssue]) -> str:
    if not issues:
        return "타임스탬프 순서·중복·영상 범위 검사를 통과했습니다."
    return "\n".join(
        f"{'전체' if issue.line_number <= 0 else f'{issue.line_number}행'} · {issue.message}"
        for issue in issues
    )
