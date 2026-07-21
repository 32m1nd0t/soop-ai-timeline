from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import numpy as np

from ..models import Vod
from .transcription import AnalysisCancelled, CancelCallback, ProgressCallback


VOD_INFO_URL = "https://api.m.sooplive.com/station/video/a/view"
VOD_ORIGIN = "https://vod.sooplive.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
WHISPER_SAMPLE_RATE = 16_000
DEFAULT_CHUNK_SECONDS = 10 * 60
DEFAULT_OVERLAP_SECONDS = 15
MAX_METADATA_BYTES = 8 * 1024 * 1024


@dataclass(slots=True, frozen=True)
class VodAudioPart:
    order: int
    duration_seconds: float
    url: str


@dataclass(slots=True, frozen=True)
class VodAudioSource:
    vod_id: str
    total_duration_seconds: float
    parts: tuple[VodAudioPart, ...]


@dataclass(slots=True, frozen=True)
class AudioChunk:
    part_order: int
    start_seconds: float
    pcm_s16: bytes
    sample_rate: int = WHISPER_SAMPLE_RATE

    @property
    def duration_seconds(self) -> float:
        return len(self.pcm_s16) / 2 / self.sample_rate

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds

    def as_float32(self) -> np.ndarray:
        samples = np.frombuffer(self.pcm_s16, dtype="<i2")
        return samples.astype(np.float32) / 32768.0


def fetch_vod_audio_source(
    vod: Vod,
    progress: ProgressCallback,
    cancelled: CancelCallback,
) -> VodAudioSource:
    """Return every public, audio-only HLS part used by SOOP's web player.

    This is a single metadata request. It does not download or persist video data,
    does not use login cookies, and refuses protected/non-public VODs.
    """
    if cancelled():
        raise AnalysisCancelled("분석을 취소했습니다.")
    if not vod.vod_id.isdigit():
        raise RuntimeError("SOOP VOD 번호가 올바르지 않습니다.")

    progress(2, "SOOP에서 공개 VOD의 고속 오디오 스트림을 확인합니다…")
    body = urlencode(
        {
            "nTitleNo": vod.vod_id,
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
            "Referer": vod.url,
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            final_url = str(response.geturl())
            if not _is_soop_https_url(final_url):
                raise RuntimeError("SOOP 밖으로 연결이 전환되어 분석을 중단했습니다.")
            raw = response.read(MAX_METADATA_BYTES + 1)
    except HTTPError as error:
        if error.code == 429:
            raise RuntimeError(
                "SOOP 요청 한도에 도달했습니다. 잠시 후 다시 시도하세요. "
                "1배속 캡처로 전환하지 않았습니다."
            ) from error
        raise RuntimeError(
            f"SOOP 고속 오디오 정보를 받지 못했습니다 (HTTP {error.code}). "
            "1배속 캡처로 전환하지 않았습니다."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(
            "SOOP 고속 오디오 정보 요청에 실패했습니다. 네트워크 상태를 확인하세요. "
            "1배속 캡처로 전환하지 않았습니다."
        ) from error

    if len(raw) > MAX_METADATA_BYTES:
        raise RuntimeError("SOOP VOD 정보 응답이 비정상적으로 커서 분석을 중단했습니다.")
    if cancelled():
        raise AnalysisCancelled("분석을 취소했습니다.")

    try:
        payload = json.loads(raw.decode("utf-8"))
        data = payload["data"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError(
            "SOOP VOD 정보 형식이 변경되어 고속 분석을 시작할 수 없습니다. "
            "1배속 캡처로 전환하지 않았습니다."
        ) from error

    if int(payload.get("result", 0) or 0) != 1 or not isinstance(data, dict):
        raise RuntimeError(_api_failure_message(payload))
    if int(data.get("is_public", 0) or 0) != 1:
        raise RuntimeError("전체 공개 VOD만 분석할 수 있습니다.")
    if bool(data.get("is_paid")) or bool(data.get("is_ppv")):
        raise RuntimeError("유료 또는 구매 제한 VOD는 분석하지 않습니다.")
    adult_status = str(data.get("adult_status", "pass") or "pass").lower()
    if adult_status not in {"pass", "none"}:
        raise RuntimeError("로그인 또는 연령 확인이 필요한 VOD는 분석하지 않습니다.")

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise RuntimeError(
            "SOOP에서 본편 파일 목록을 받지 못했습니다. "
            "1배속 캡처로 전환하지 않았습니다."
        )

    parts: list[VodAudioPart] = []
    for index, item in enumerate(raw_files, start=1):
        if not isinstance(item, dict):
            raise RuntimeError("SOOP 본편 파일 정보가 올바르지 않습니다.")
        if str(item.get("hide", "N") or "N").upper() != "N":
            raise RuntimeError("숨김 처리된 본편 구간이 있어 불완전한 분석을 중단했습니다.")

        radio_url = str(item.get("radio_url", "") or "").strip()
        if not radio_url or not _is_soop_https_url(radio_url):
            raise RuntimeError(
                "오디오 전용 스트림이 없는 본편 구간이 있어 분석을 중단했습니다. "
                "영상 스트림이나 1배속 캡처로 전환하지 않았습니다."
            )
        try:
            duration_seconds = max(0.0, float(item.get("duration", 0) or 0) / 1000.0)
            order = int(item.get("file_order", index) or index)
        except (TypeError, ValueError) as error:
            raise RuntimeError("SOOP 본편 재생시간 정보가 올바르지 않습니다.") from error
        parts.append(VodAudioPart(order, duration_seconds, radio_url))

    parts.sort(key=lambda item: item.order)
    summed_duration = sum(part.duration_seconds for part in parts)
    try:
        declared_duration = max(
            0.0, float(data.get("total_file_duration", 0) or 0) / 1000.0
        )
    except (TypeError, ValueError):
        declared_duration = 0.0
    total_duration = declared_duration or summed_duration
    if total_duration <= 0:
        raise RuntimeError("SOOP VOD 전체 재생시간을 확인하지 못했습니다.")

    progress(
        8,
        f"오디오 전용 스트림 {len(parts):,}개 확인 · "
        f"총 {format_timestamp(total_duration)} · 영상 데이터는 받지 않습니다.",
    )
    return VodAudioSource(vod.vod_id, total_duration, tuple(parts))


def iter_audio_chunks(
    source: VodAudioSource,
    cancelled: CancelCallback,
    *,
    chunk_seconds: int = DEFAULT_CHUNK_SECONDS,
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
    start_seconds: float = 0.0,
) -> Iterator[AudioChunk]:
    """Decode audio-only HLS into bounded in-memory PCM chunks."""
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds must be between 0 and chunk_seconds")

    resume_at = max(0.0, float(start_seconds or 0.0))
    part_offset = 0.0
    for part in source.parts:
        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
        if part_offset + part.duration_seconds <= resume_at:
            part_offset += part.duration_seconds
            continue
        yield from _iter_part_audio_chunks(
            part,
            part_offset,
            cancelled,
            chunk_seconds=chunk_seconds,
            overlap_seconds=overlap_seconds,
            skip_seconds=max(0.0, resume_at - part_offset),
        )
        part_offset += part.duration_seconds


def _iter_part_audio_chunks(
    part: VodAudioPart,
    part_offset: float,
    cancelled: CancelCallback,
    *,
    chunk_seconds: int,
    overlap_seconds: int,
    skip_seconds: float = 0.0,
) -> Iterator[AudioChunk]:
    try:
        import av
    except ImportError as error:
        raise RuntimeError("PyAV가 설치되지 않아 고속 스트림을 읽을 수 없습니다.") from error

    target_bytes = chunk_seconds * WHISPER_SAMPLE_RATE * 2
    overlap_bytes = overlap_seconds * WHISPER_SAMPLE_RATE * 2
    advance_bytes = target_bytes - overlap_bytes
    buffer = bytearray()
    skip_bytes = max(0, int(skip_seconds * WHISPER_SAMPLE_RATE) * 2)
    local_start_samples = skip_bytes // 2
    emitted = False
    options = {
        "headers": (
            f"Referer: {VOD_ORIGIN}/\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
        ),
        "rw_timeout": "20000000",
        "reconnect": "1",
        "reconnect_streamed": "1",
        "reconnect_delay_max": "5",
    }

    try:
        with av.open(part.url, options=options) as container:
            audio_streams = [stream for stream in container.streams if stream.type == "audio"]
            video_streams = [stream for stream in container.streams if stream.type == "video"]
            if not audio_streams:
                raise RuntimeError("오디오 트랙을 찾지 못했습니다.")
            if video_streams:
                raise RuntimeError("오디오 전용이 아닌 스트림이 반환되어 분석을 중단했습니다.")

            stream = audio_streams[0]
            resampler = av.AudioResampler(
                format="s16",
                layout="mono",
                rate=WHISPER_SAMPLE_RATE,
            )
            for packet in container.demux(stream):
                if cancelled():
                    raise AnalysisCancelled("분석을 취소했습니다.")
                for frame in packet.decode():
                    for converted in _resampled_frames(resampler.resample(frame)):
                        frame_bytes = _audio_frame_to_s16(converted)
                        if skip_bytes:
                            removed = min(skip_bytes, len(frame_bytes))
                            frame_bytes = frame_bytes[removed:]
                            skip_bytes -= removed
                        if not frame_bytes:
                            continue
                        buffer.extend(frame_bytes)
                        while len(buffer) >= target_bytes:
                            chunk_bytes = bytes(buffer[:target_bytes])
                            yield AudioChunk(
                                part_order=part.order,
                                start_seconds=(
                                    part_offset
                                    + local_start_samples / WHISPER_SAMPLE_RATE
                                ),
                                pcm_s16=chunk_bytes,
                            )
                            emitted = True
                            del buffer[:advance_bytes]
                            local_start_samples += advance_bytes // 2

            for converted in _resampled_frames(resampler.resample(None)):
                frame_bytes = _audio_frame_to_s16(converted)
                if skip_bytes:
                    removed = min(skip_bytes, len(frame_bytes))
                    frame_bytes = frame_bytes[removed:]
                    skip_bytes -= removed
                buffer.extend(frame_bytes)
    except AnalysisCancelled:
        raise
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"SOOP 오디오 스트림 {part.order}번 구간을 고속으로 읽지 못했습니다. "
            "1배속 캡처로 전환하지 않았습니다."
        ) from error

    minimum_final_bytes = overlap_bytes if emitted else 0
    if len(buffer) > minimum_final_bytes:
        yield AudioChunk(
            part_order=part.order,
            start_seconds=part_offset + local_start_samples / WHISPER_SAMPLE_RATE,
            pcm_s16=bytes(buffer),
        )


def _resampled_frames(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _audio_frame_to_s16(frame: object) -> bytes:
    array = np.asarray(frame.to_ndarray())
    if array.size == 0:
        return b""
    if array.dtype != np.int16:
        array = array.astype(np.int16)
    return np.ascontiguousarray(array.reshape(-1), dtype="<i2").tobytes()


def _is_soop_https_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (
        host == "sooplive.com" or host.endswith(".sooplive.com")
    )


def _api_failure_message(payload: object) -> str:
    message = ""
    if isinstance(payload, dict):
        for key in ("message", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                message = value.strip()
                break
    suffix = f" ({message})" if message else ""
    return (
        "SOOP에서 공개 VOD 정보를 받지 못했습니다"
        f"{suffix}. 1배속 캡처로 전환하지 않았습니다."
    )


def format_timestamp(seconds: float) -> str:
    total = max(0, int(math.ceil(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
