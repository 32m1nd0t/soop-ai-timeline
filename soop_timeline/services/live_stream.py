from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .transcription import CancelCallback
from .vod_stream import (
    MAX_METADATA_BYTES,
    USER_AGENT,
    AudioChunk,
    WHISPER_SAMPLE_RATE,
    _audio_frame_to_s16,
    _is_soop_https_url,
    _resampled_frames,
)


LIVE_INFO_URL = "https://live.sooplive.com/afreeca/player_live_api.php"
LIVE_ORIGIN = "https://play.sooplive.com"
DEFAULT_LIVE_CHUNK_SECONDS = 15
DEFAULT_LIVE_OVERLAP_SECONDS = 2


@dataclass(slots=True, frozen=True)
class LiveAudioSource:
    kind: str
    channel_id: str
    streamer_name: str
    broadcast_no: str
    title: str
    page_url: str
    runtime_seconds: float
    stream_url: str


def fetch_live_audio_source(
    channel_id: str,
    broadcast_no: str,
    page_url: str,
    cancelled: CancelCallback,
) -> LiveAudioSource:
    request_started = time.monotonic()
    live = _request_live_info(
        channel_id,
        broadcast_no,
        "live",
        "HD",
        page_url,
        cancelled,
    )
    if int(live.get("RESULT", 0) or 0) != 1:
        raise RuntimeError(_live_failure_message(live))
    resolved_broadcast_no = str(live.get("BNO", "") or "").strip()
    if not resolved_broadcast_no.isdigit():
        raise RuntimeError("현재 진행 중인 공개 라이브 방송을 찾지 못했습니다.")
    if broadcast_no and resolved_broadcast_no != broadcast_no:
        raise RuntimeError("입력한 라이브 방송이 종료되었거나 다른 방송으로 전환되었습니다.")
    if str(live.get("BPWD", "N") or "N").upper() == "Y":
        raise RuntimeError("비밀번호가 필요한 라이브 방송은 분석하지 않습니다.")
    if str(live.get("BSTATUS", "") or "").upper() == "BROAD_HIDE":
        raise RuntimeError("숨김 라이브 방송은 분석하지 않습니다.")
    try:
        grade = int(live.get("GRADE", 0) or 0)
    except (TypeError, ValueError):
        grade = 0
    if grade >= 19:
        raise RuntimeError("연령 확인이 필요한 라이브 방송은 분석하지 않습니다.")
    try:
        minimum_tier = int(live.get("P_MIN_TIER", 0) or 0)
    except (TypeError, ValueError):
        minimum_tier = 0
    if minimum_tier > 0:
        raise RuntimeError("구독자 전용 라이브 방송은 분석하지 않습니다.")

    aid_info = _request_live_info(
        channel_id,
        resolved_broadcast_no,
        "aid",
        "SD",
        page_url,
        cancelled,
    )
    aid = str(aid_info.get("AID", "") or "").strip()
    if int(aid_info.get("RESULT", 0) or 0) != 1 or not aid:
        raise RuntimeError("SOOP 라이브 재생 인증 정보를 받지 못했습니다.")

    resource_domain = str(live.get("RMD", "") or "").strip().rstrip("/")
    if not _is_soop_https_url(resource_domain):
        raise RuntimeError("SOOP 라이브 스트림 관리 주소가 올바르지 않습니다.")
    cdn_type = _stream_manager_cdn_type(str(live.get("CDN", "") or ""))
    manager_query = urlencode(
        {
            "return_type": cdn_type,
            "use_cors": "true",
            "cors_origin_url": "play.sooplive.com",
            "broad_key": f"{resolved_broadcast_no}-common-sd-hls",
            "player_mode": "live",
            "time": f"{time.time() % 10_000:.6f}",
        }
    )
    manager_url = f"{resource_domain}/broad_stream_assign.html?{manager_query}"
    manager = _request_json(
        Request(manager_url, headers=_live_headers(page_url)),
        "SOOP 라이브 재생 주소를 받지 못했습니다.",
    )
    stream_url = str(manager.get("view_url", "") or "").strip()
    if int(manager.get("result", 0) or 0) != 1 or not _is_soop_https_url(stream_url):
        raise RuntimeError("SOOP 라이브 재생 주소가 올바르지 않습니다.")
    separator = "&" if urlparse(stream_url).query else "?"
    stream_url = f"{stream_url}{separator}{urlencode({'aid': aid})}"

    try:
        runtime_seconds = max(0.0, float(live.get("BTIME", 0) or 0))
    except (TypeError, ValueError):
        runtime_seconds = 0.0
    runtime_seconds += max(0.0, time.monotonic() - request_started)
    canonical_url = f"https://play.sooplive.com/{channel_id}/{resolved_broadcast_no}"
    return LiveAudioSource(
        kind="live",
        channel_id=str(live.get("BJID") or channel_id).strip(),
        streamer_name=str(live.get("BJNICK") or channel_id).strip(),
        broadcast_no=resolved_broadcast_no,
        title=str(live.get("TITLE") or "SOOP 라이브").strip(),
        page_url=canonical_url,
        runtime_seconds=runtime_seconds,
        stream_url=stream_url,
    )


def iter_live_audio_chunks(
    source: LiveAudioSource,
    stop_requested: CancelCallback,
    *,
    chunk_seconds: int = DEFAULT_LIVE_CHUNK_SECONDS,
    overlap_seconds: int = DEFAULT_LIVE_OVERLAP_SECONDS,
) -> Iterator[AudioChunk]:
    """Decode a public live HLS stream into bounded, in-memory audio chunks.

    SOOP's public live HLS currently multiplexes video and audio. FFmpeg receives
    the low-quality transport stream, but this iterator decodes only the audio
    track and never writes media data to disk.
    """
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be between 0 and chunk_seconds")
    try:
        import av
    except ImportError as error:
        raise RuntimeError("PyAV가 설치되지 않아 라이브 오디오를 읽을 수 없습니다.") from error

    target_bytes = chunk_seconds * WHISPER_SAMPLE_RATE * 2
    overlap_bytes = overlap_seconds * WHISPER_SAMPLE_RATE * 2
    advance_bytes = target_bytes - overlap_bytes
    buffer = bytearray()
    local_start_samples = 0
    emitted = False
    options = {
        "headers": (
            f"Referer: {LIVE_ORIGIN}/\r\n"
            f"Origin: {LIVE_ORIGIN}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
        ),
        "rw_timeout": "8000000",
        "reconnect": "1",
        "reconnect_streamed": "1",
        "reconnect_at_eof": "1",
        "reconnect_delay_max": "5",
        "live_start_index": "-1",
    }
    try:
        with av.open(source.stream_url, options=options) as container:
            audio_streams = [stream for stream in container.streams if stream.type == "audio"]
            if not audio_streams:
                raise RuntimeError("라이브 방송의 오디오 트랙을 찾지 못했습니다.")
            stream = audio_streams[0]
            resampler = av.AudioResampler(
                format="s16",
                layout="mono",
                rate=WHISPER_SAMPLE_RATE,
            )
            for packet in container.demux(stream):
                if stop_requested():
                    break
                for frame in packet.decode():
                    for converted in _resampled_frames(resampler.resample(frame)):
                        buffer.extend(_audio_frame_to_s16(converted))
                        while len(buffer) >= target_bytes:
                            yield AudioChunk(
                                part_order=1,
                                start_seconds=(
                                    source.runtime_seconds
                                    + local_start_samples / WHISPER_SAMPLE_RATE
                                ),
                                pcm_s16=bytes(buffer[:target_bytes]),
                            )
                            emitted = True
                            del buffer[:advance_bytes]
                            local_start_samples += advance_bytes // 2
                            if stop_requested():
                                break
                    if stop_requested():
                        break
                if stop_requested():
                    break
            for converted in _resampled_frames(resampler.resample(None)):
                buffer.extend(_audio_frame_to_s16(converted))
    except RuntimeError:
        raise
    except Exception as error:
        if stop_requested():
            return
        raise RuntimeError("SOOP 라이브 오디오 연결이 끊겼습니다.") from error

    minimum_final_bytes = overlap_bytes if emitted else WHISPER_SAMPLE_RATE * 2
    if len(buffer) > minimum_final_bytes:
        yield AudioChunk(
            part_order=1,
            start_seconds=(
                source.runtime_seconds + local_start_samples / WHISPER_SAMPLE_RATE
            ),
            pcm_s16=bytes(buffer),
        )


def _request_live_info(
    channel_id: str,
    broadcast_no: str,
    request_type: str,
    quality: str,
    page_url: str,
    cancelled: CancelCallback,
) -> dict[str, object]:
    if cancelled():
        raise RuntimeError("링크 확인을 취소했습니다.")
    query_url = f"{LIVE_INFO_URL}?{urlencode({'bjid': channel_id})}"
    body = urlencode(
        {
            "bid": channel_id,
            "bno": broadcast_no,
            "type": request_type,
            "pwd": "",
            "player_type": "html5",
            "stream_type": "common",
            "quality": quality,
            "mode": "live",
            "from_api": "0",
            "is_revive": "false",
        }
    ).encode("ascii")
    headers = _live_headers(page_url)
    headers.update(
        {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    payload = _request_json(
        Request(query_url, data=body, headers=headers, method="POST"),
        "SOOP 라이브 정보를 받지 못했습니다.",
    )
    channel = payload.get("CHANNEL")
    if not isinstance(channel, dict):
        raise RuntimeError("SOOP 라이브 정보 형식이 변경되었습니다.")
    return channel


def _request_json(request: Request, failure_message: str) -> dict[str, object]:
    try:
        with urlopen(request, timeout=20) as response:
            final_url = str(response.geturl())
            if not _is_soop_https_url(final_url):
                raise RuntimeError("SOOP 밖으로 연결이 전환되어 작업을 중단했습니다.")
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        raise RuntimeError(f"{failure_message} (HTTP {error.code})") from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(failure_message) from error
    if len(raw) > MAX_METADATA_BYTES:
        raise RuntimeError("SOOP 응답이 비정상적으로 커서 작업을 중단했습니다.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("SOOP 라이브 응답 형식이 변경되었습니다.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("SOOP 라이브 응답 형식이 올바르지 않습니다.")
    return payload


def _live_headers(page_url: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": LIVE_ORIGIN,
        "Referer": page_url,
        "User-Agent": USER_AGENT,
    }


def _stream_manager_cdn_type(value: str) -> str:
    normalized = value.strip().lower()
    if "gs_cdn" in normalized:
        return "gs_cdn_pc_web"
    if "lg_cdn" in normalized or not normalized:
        return "lg_cdn_pc_web"
    if re_safe_cdn_type(normalized):
        return normalized
    raise RuntimeError("SOOP 라이브 CDN 정보가 올바르지 않습니다.")


def re_safe_cdn_type(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character == "_" for character in value)


def _live_failure_message(channel: dict[str, object]) -> str:
    message = str(channel.get("MSG", "") or "").strip()
    if message:
        return f"현재 공개 라이브 방송을 열 수 없습니다 ({message})."
    return "현재 진행 중인 공개 라이브 방송을 찾지 못했습니다."
