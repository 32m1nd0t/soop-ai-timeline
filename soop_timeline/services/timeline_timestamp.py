from __future__ import annotations

import re
from dataclasses import dataclass


TIMESTAMP_PATTERN = re.compile(
    r"(?<!\d)(?:(?P<hours>\d{1,3}):)?(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)(?!\d)"
)
LINE_TIMESTAMP_PATTERN = re.compile(
    r"^(?P<indent>\s*)(?P<timestamp>(?:\d{1,3}:)?[0-5]\d:[0-5]\d)(?=\s|$)"
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


def shift_all_timestamps(text: str, offset_seconds: int) -> tuple[str, int]:
    """Shift only timestamps that begin timeline lines."""
    changed = 0
    lines = text.splitlines(keepends=True)
    updated: list[str] = []
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = LINE_TIMESTAMP_PATTERN.match(body)
        if match is None:
            updated.append(line)
            continue
        seconds = parse_timestamp(match.group("timestamp"))
        if seconds is None:
            updated.append(line)
            continue
        replacement = format_timestamp_seconds(seconds + int(offset_seconds))
        updated.append(body[: match.start("timestamp")] + replacement + body[match.end("timestamp") :] + ending)
        changed += 1
    if not lines and text:
        return text, 0
    return "".join(updated), changed


def adjust_timestamp_on_current_line(
    text: str,
    cursor_position: int,
    offset_seconds: int,
) -> tuple[str, bool]:
    position = max(0, min(len(text), int(cursor_position)))
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end].rstrip("\r")
    match = LINE_TIMESTAMP_PATTERN.match(line)
    if match is None:
        return text, False
    seconds = parse_timestamp(match.group("timestamp"))
    if seconds is None:
        return text, False
    replacement = format_timestamp_seconds(seconds + int(offset_seconds))
    start = line_start + match.start("timestamp")
    end = line_start + match.end("timestamp")
    return text[:start] + replacement + text[end:], True


def merge_current_timeline_line_with_previous(
    text: str,
    cursor_position: int,
) -> tuple[str, bool]:
    """Merge the current timestamp summary into the previous timestamp line."""
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    if not lines:
        return text, False
    position = max(0, min(len(text), int(cursor_position)))
    current_index = text[:position].count("\n")
    current_index = min(current_index, len(lines) - 1)
    current_match = LINE_TIMESTAMP_PATTERN.match(lines[current_index])
    if current_match is None:
        return text, False
    previous_index = current_index - 1
    while previous_index >= 0 and not lines[previous_index].strip():
        previous_index -= 1
    if previous_index < 0:
        return text, False
    previous_match = LINE_TIMESTAMP_PATTERN.match(lines[previous_index])
    if previous_match is None:
        return text, False

    previous_summary = lines[previous_index][previous_match.end() :].strip()
    current_summary = lines[current_index][current_match.end() :].strip()
    if not current_summary:
        return text, False
    prefix = lines[previous_index][: previous_match.end()]
    if previous_summary:
        prefix += f" {previous_summary}"
    lines[previous_index] = prefix + (" · " if previous_summary else " ") + current_summary
    del lines[current_index]
    updated = "\n".join(lines)
    if trailing_newline:
        updated += "\n"
    return updated, True
