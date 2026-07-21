from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,80}$")


def normalize_channel_id(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("스트리머 아이디 또는 방송국 주소를 입력하세요.")

    candidate = raw
    if "://" in raw:
        parsed = urlparse(raw)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "station" in parts:
            station_index = parts.index("station")
            if station_index + 1 < len(parts):
                candidate = parts[station_index + 1]
        elif parts:
            candidate = parts[0]

    candidate = candidate.strip().strip("/")
    if not CHANNEL_ID_PATTERN.fullmatch(candidate):
        raise ValueError("SOOP 스트리머 아이디 형식이 올바르지 않습니다.")
    return candidate

