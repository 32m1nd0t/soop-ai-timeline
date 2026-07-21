from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from .transcription import format_timestamp
from .vod_stream import MAX_METADATA_BYTES, USER_AGENT, VOD_INFO_URL, VOD_ORIGIN


VOD_PATH = re.compile(r"^/player/(?P<vod_id>\d+)(?:[/?#]|$)")
LIVE_PATH = re.compile(
    r"^/(?P<channel_id>[A-Za-z0-9_-]+)(?:/(?P<broadcast_no>\d+))?(?:[/?#]|$)"
)
VOD_HOSTS = {
    "vod.sooplive.com",
    "vod.sooplive.co.kr",
    "vod.afreecatv.com",
}
LIVE_HOSTS = {
    "play.sooplive.com",
    "play.sooplive.co.kr",
    "play.afreecatv.com",
}


@dataclass(slots=True, frozen=True)
class ParsedSoopLink:
    kind: str
    page_url: str
    vod_id: str = ""
    channel_id: str = ""
    broadcast_no: str = ""


@dataclass(slots=True, frozen=True)
class ResolvedVodLink:
    kind: str
    vod_id: str
    channel_id: str
    streamer_name: str
    title: str
    page_url: str
    duration_text: str
    published_text: str
    thumbnail_url: str


def parse_soop_link(value: str) -> ParsedSoopLink:
    text = value.strip()
    if not text:
        raise ValueError("SOOP 다시보기 또는 라이브 링크를 입력하세요.")
    if "://" not in text:
        text = f"https://{text}"
    try:
        parsed = urlparse(text)
    except ValueError as error:
        raise ValueError("링크 형식이 올바르지 않습니다.") from error

    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("http 또는 https SOOP 링크만 사용할 수 있습니다.")

    if host in VOD_HOSTS:
        match = VOD_PATH.match(parsed.path)
        if match is None:
            raise ValueError("SOOP 다시보기 재생 링크를 확인하세요.")
        vod_id = match.group("vod_id")
        return ParsedSoopLink(
            kind="vod",
            page_url=f"https://vod.sooplive.com/player/{vod_id}",
            vod_id=vod_id,
        )

    if host in LIVE_HOSTS:
        match = LIVE_PATH.match(parsed.path)
        if match is None:
            raise ValueError("SOOP 라이브 재생 링크를 확인하세요.")
        channel_id = unquote(match.group("channel_id")).strip()
        if not channel_id:
            raise ValueError("라이브 링크에서 스트리머 아이디를 찾지 못했습니다.")
        broadcast_no = match.group("broadcast_no") or ""
        suffix = f"/{broadcast_no}" if broadcast_no else ""
        return ParsedSoopLink(
            kind="live",
            page_url=f"https://play.sooplive.com/{channel_id}{suffix}",
            channel_id=channel_id,
            broadcast_no=broadcast_no,
        )

    raise ValueError("SOOP 다시보기(vod.sooplive.com) 또는 라이브(play.sooplive.com) 링크만 지원합니다.")


def resolve_manual_link(value: str, cancelled=lambda: False) -> object:
    parsed = parse_soop_link(value)
    if parsed.kind == "vod":
        return resolve_vod_link(parsed, cancelled)
    from .live_stream import fetch_live_audio_source

    return fetch_live_audio_source(
        parsed.channel_id,
        parsed.broadcast_no,
        parsed.page_url,
        cancelled,
    )


def resolve_vod_link(
    parsed: ParsedSoopLink,
    cancelled=lambda: False,
) -> ResolvedVodLink:
    if cancelled():
        raise RuntimeError("링크 확인을 취소했습니다.")
    body = urlencode(
        {
            "nTitleNo": parsed.vod_id,
            "nApiLevel": "11",
            "nPlaylistIdx": "0",
        }
    ).encode("ascii")
    request = Request(
        VOD_INFO_URL,
        data=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": VOD_ORIGIN,
            "Referer": parsed.page_url,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        raise RuntimeError(
            f"SOOP 다시보기 정보를 받지 못했습니다 (HTTP {error.code})."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError("SOOP 다시보기 정보 요청에 실패했습니다.") from error

    if len(raw) > MAX_METADATA_BYTES:
        raise RuntimeError("SOOP 다시보기 정보 응답이 비정상적으로 큽니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
        data = payload["data"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError("SOOP 다시보기 정보 형식이 변경되었습니다.") from error
    if int(payload.get("result", 0) or 0) != 1 or not isinstance(data, dict):
        raise RuntimeError("해당 SOOP 다시보기를 찾지 못했습니다.")
    if int(data.get("is_public", 0) or 0) != 1:
        raise RuntimeError("전체 공개 다시보기만 수동으로 분석할 수 있습니다.")
    if bool(data.get("is_paid")) or bool(data.get("is_ppv")):
        raise RuntimeError("유료 또는 구매 제한 다시보기는 분석하지 않습니다.")
    if str(data.get("adult_status", "pass") or "pass").lower() not in {
        "pass",
        "none",
    }:
        raise RuntimeError("로그인 또는 연령 확인이 필요한 다시보기는 분석하지 않습니다.")

    try:
        duration_seconds = max(
            0.0,
            float(data.get("total_file_duration", 0) or 0) / 1000.0,
        )
    except (TypeError, ValueError):
        duration_seconds = 0.0
    channel_id = str(
        data.get("writer_id") or data.get("bj_id") or "manual"
    ).strip()
    streamer_name = str(data.get("writer_nick") or channel_id or "수동 입력").strip()
    title = str(data.get("title") or data.get("full_title") or "수동 다시보기").strip()
    return ResolvedVodLink(
        kind="vod",
        vod_id=parsed.vod_id,
        channel_id=channel_id or "manual",
        streamer_name=streamer_name or "수동 입력",
        title=title or f"SOOP 다시보기 {parsed.vod_id}",
        page_url=parsed.page_url,
        duration_text=format_timestamp(duration_seconds) if duration_seconds else "",
        published_text=str(data.get("write_tm") or "").strip(),
        thumbnail_url=str(data.get("thumb") or "").strip(),
    )
