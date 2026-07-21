from __future__ import annotations


COMMENT_LIMIT = 5_000


def split_timeline(text: str, limit: int = COMMENT_LIMIT) -> list[str]:
    """Split text without data loss, preferring complete line boundaries.

    Concatenating the returned blocks always recreates the original text exactly.
    A single line longer than the limit is split only as a last resort.
    """
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    if text == "":
        return [""]
    if len(text) <= limit:
        return [text]

    blocks: list[str] = []
    current = ""

    for line in text.splitlines(keepends=True):
        remaining = line
        while remaining:
            room = limit - len(current)
            if room == 0:
                blocks.append(current)
                current = ""
                room = limit

            if len(remaining) <= room:
                current += remaining
                remaining = ""
                continue

            if current:
                # Keep the full timeline entry together when it can fit in a new block.
                if len(remaining) <= limit:
                    blocks.append(current)
                    current = ""
                    continue
                blocks.append(current)
                current = ""
                room = limit

            current = remaining[:room]
            remaining = remaining[room:]
            if len(current) == limit:
                blocks.append(current)
                current = ""

    # splitlines() omits the final empty item but not its newline, so no extra work is needed.
    if current or not blocks:
        blocks.append(current)

    return blocks


def block_label(index: int, total: int) -> str:
    if index < 0 or index >= total:
        raise IndexError(index)
    if index == 0:
        return f"댓글 · {index + 1}/{total}"
    return f"대댓글 {index} · {index + 1}/{total}"

