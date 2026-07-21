from __future__ import annotations

import re


TextMatch = tuple[int, int]


def find_literal_matches(
    text: str,
    query: str,
    *,
    case_sensitive: bool = False,
) -> list[TextMatch]:
    """Return non-overlapping literal match spans in document order."""
    if not query:
        return []
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    return [(match.start(), match.end()) for match in pattern.finditer(text)]


def replace_literal_all(
    text: str,
    query: str,
    replacement: str,
    *,
    case_sensitive: bool = False,
) -> tuple[str, int]:
    """Replace every literal match and return the new text and replacement count."""
    if not query:
        return text, 0
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(query), flags)
    return pattern.subn(lambda _: replacement, text)
