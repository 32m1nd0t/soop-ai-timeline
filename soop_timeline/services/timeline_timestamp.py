from __future__ import annotations

import re
from dataclasses import dataclass


TIMESTAMP_PATTERN = re.compile(
    r"(?<!\d)(?:(?P<hours>\d{1,3}):)?(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)(?!\d)"
)


@dataclass(frozen=True, slots=True)
class TimestampHit:
    seconds: int
    start: int
    end: int
    text: str


def parse_timestamp(value: str) -> int | None:
    match = TIMESTAMP_PATTERN.fullmatch(value.strip())
    if match is None:
        return None
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    return hours * 3_600 + minutes * 60 + seconds


def timestamp_at_position(line: str, position: int) -> TimestampHit | None:
    if position < 0:
        return None
    for match in TIMESTAMP_PATTERN.finditer(line):
        # QTextCursor can report the position immediately after the final digit.
        if match.start() <= position <= match.end():
            seconds = parse_timestamp(match.group(0))
            if seconds is None:
                continue
            return TimestampHit(
                seconds=seconds,
                start=match.start(),
                end=match.end(),
                text=match.group(0),
            )
    return None


def format_timestamp_seconds(seconds: int) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
