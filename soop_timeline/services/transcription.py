from __future__ import annotations

import json
import os
import queue
import threading
import ctypes
import importlib.util
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, TYPE_CHECKING

from .eta import EtaEstimator, format_eta

if TYPE_CHECKING:
    from .live_stream import LiveAudioSource
    from .vod_stream import AudioChunk, VodAudioSource


ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]
PreviewCallback = Callable[[str, str], None]
LiveUpdateCallback = Callable[["Transcript"], None]


_NVIDIA_DLL_DIRECTORY_HANDLES: list[object] = []
_NVIDIA_RUNTIME_PATHS_CONFIGURED = False


def configure_nvidia_runtime_paths() -> tuple[Path, ...]:
    """Expose NVIDIA pip-wheel DLLs to Windows' native loader.

    The CUDA runtime wheels keep their DLLs inside package-local ``bin``
    directories, which are not part of the default Windows DLL search path.
    Keep the directory handles alive for the lifetime of this process.
    """
    global _NVIDIA_RUNTIME_PATHS_CONFIGURED
    if os.name != "nt" or _NVIDIA_RUNTIME_PATHS_CONFIGURED:
        return tuple()

    discovered: list[Path] = []
    for package_name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(package_name)
        except (ImportError, ModuleNotFoundError, AttributeError):
            spec = None
        if spec is None:
            continue

        locations = spec.submodule_search_locations or []
        for location in locations:
            bin_directory = Path(location) / "bin"
            if bin_directory.is_dir() and bin_directory not in discovered:
                discovered.append(bin_directory)

    existing_path_entries = os.environ.get("PATH", "").split(os.pathsep)
    for directory in discovered:
        directory_text = str(directory)
        if directory_text not in existing_path_entries:
            os.environ["PATH"] = directory_text + os.pathsep + os.environ.get("PATH", "")
            existing_path_entries.insert(0, directory_text)
        try:
            _NVIDIA_DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(directory_text))
        except (AttributeError, FileNotFoundError, OSError):
            continue

    _NVIDIA_RUNTIME_PATHS_CONFIGURED = True
    return tuple(discovered)


configure_nvidia_runtime_paths()


class AnalysisCancelled(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class WhisperRuntime:
    device: str
    compute_type: str
    description: str
    warning: str = ""


def detect_whisper_runtime(preference: str = "auto") -> WhisperRuntime:
    requested = preference if preference in {"auto", "cuda", "cpu"} else "auto"
    cuda_device_count = 0
    try:
        import ctranslate2

        cuda_device_count = ctranslate2.get_cuda_device_count()
    except Exception:
        cuda_device_count = 0

    runtime_libraries_ready = True
    if os.name == "nt" and cuda_device_count:
        for library_name in ("cublas64_12.dll", "cudnn64_9.dll"):
            try:
                ctypes.WinDLL(library_name)
            except OSError:
                runtime_libraries_ready = False
                break

    cuda_ready = cuda_device_count > 0 and runtime_libraries_ready
    if requested == "cuda" and not cuda_ready:
        raise RuntimeError(
            "NVIDIA GPU는 감지됐지만 faster-whisper에 필요한 CUDA 12 cuBLAS와 "
            "cuDNN 9 런타임을 찾지 못했습니다. AI 설정을 '자동' 또는 'CPU'로 "
            "바꾸거나 CUDA 런타임을 설치하세요."
        )
    if requested == "cuda" or (requested == "auto" and cuda_ready):
        return WhisperRuntime(
            device="cuda",
            compute_type="float16",
            description="NVIDIA GPU · CUDA float16",
        )

    warning = ""
    if requested == "auto" and cuda_device_count and not runtime_libraries_ready:
        warning = (
            "CUDA 12 cuBLAS·cuDNN 9 런타임이 없어 CPU로 대체합니다. "
            "긴 영상은 large-v3-turbo를 권장합니다."
        )
    return WhisperRuntime(
        device="cpu",
        compute_type="int8",
        description="CPU · int8",
        warning=warning,
    )


@dataclass(slots=True, frozen=True)
class TranscriptSegment:
    segment_id: str
    start: float
    end: float
    text: str


@dataclass(slots=True)
class Transcript:
    model: str
    language: str
    duration_seconds: float
    segments: list[TranscriptSegment]

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "segments": [asdict(segment) for segment in self.segments],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Transcript":
        raw_segments = value.get("segments", [])
        segments = [
            TranscriptSegment(
                segment_id=str(item["segment_id"]),
                start=float(item["start"]),
                end=float(item["end"]),
                text=str(item["text"]),
            )
            for item in raw_segments
            if isinstance(item, dict)
        ]
        return cls(
            model=str(value.get("model", "")),
            language=str(value.get("language", "ko")),
            duration_seconds=float(value.get("duration_seconds", 0.0)),
            segments=segments,
        )


@dataclass(slots=True)
class _WhisperBackend:
    runtime: WhisperRuntime
    model: object
    batched_pipeline: object


@dataclass(slots=True, frozen=True)
class _StreamFailure:
    error: BaseException


_STREAM_END = object()
_MODEL_CACHE: dict[tuple[str, str], _WhisperBackend] = {}
_MODEL_LOCK = threading.Lock()


class FasterWhisperTranscriber:
    def __init__(self, model_name: str = "large-v3-turbo", device: str = "auto"):
        self.model_name = model_name
        self.device_preference = device

    def _prepare_backend(
        self,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> _WhisperBackend:
        try:
            from faster_whisper import BatchedInferencePipeline, WhisperModel
        except ImportError as error:
            raise RuntimeError(
                "faster-whisper가 설치되지 않았습니다. 프로그램 의존성을 다시 설치하세요."
            ) from error

        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")

        runtime = detect_whisper_runtime(self.device_preference)
        runtime_note = f" · {runtime.warning}" if runtime.warning else ""
        progress(
            2,
            f"Whisper {self.model_name} 모델을 준비합니다 ({runtime.description}). "
            f"처음 한 번은 모델 다운로드가 필요합니다…{runtime_note}",
        )
        cache_key = (self.model_name, f"{runtime.device}:{runtime.compute_type}")
        with _MODEL_LOCK:
            backend = _MODEL_CACHE.get(cache_key)
            if backend is None:
                model = WhisperModel(
                    self.model_name,
                    device=runtime.device,
                    compute_type=runtime.compute_type,
                )
                backend = _WhisperBackend(
                    runtime=runtime,
                    model=model,
                    batched_pipeline=BatchedInferencePipeline(model=model),
                )
                _MODEL_CACHE[cache_key] = backend

        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
        return backend

    def transcribe(
        self,
        media_path: str | Path,
        initial_prompt: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> Transcript:
        path = Path(media_path)
        if not path.is_file():
            raise FileNotFoundError(f"분석할 파일을 찾을 수 없습니다: {path}")

        backend = self._prepare_backend(progress, cancelled)
        progress(5, f"로컬 {backend.runtime.description}로 음성을 인식합니다…")
        raw_segments, info = backend.model.transcribe(
            str(path),
            language="ko",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt,
        )

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        language = str(getattr(info, "language", "ko") or "ko")
        segments: list[TranscriptSegment] = []
        last_percent = -1
        last_preview_at = 0.0
        eta = EtaEstimator(duration)
        for raw in raw_segments:
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            text = str(raw.text).strip()
            if text:
                segments.append(
                    TranscriptSegment(
                        segment_id=f"s{len(segments):06d}",
                        start=max(0.0, float(raw.start)),
                        end=max(0.0, float(raw.end)),
                        text=text,
                    )
                )
                now = time.monotonic()
                if preview is not None and (
                    len(segments) == 1
                    or len(segments) % 25 == 0
                    or now - last_preview_at >= 2.0
                ):
                    preview("transcript", transcript_preview_document(segments))
                    last_preview_at = now
            if duration > 0:
                percent = min(68, 5 + int((float(raw.end) / duration) * 63))
                if percent != last_percent:
                    processed_seconds = float(raw.end)
                    progress(
                        percent,
                        "음성 인식 중… "
                        f"{format_timestamp(processed_seconds)} / "
                        f"{format_timestamp(duration)} · "
                        f"{format_eta(eta.remaining_seconds(processed_seconds))}",
                    )
                    last_percent = percent

        if not segments:
            raise RuntimeError("음성을 인식하지 못했습니다. 파일에 재생 가능한 음성이 있는지 확인하세요.")

        if preview is not None:
            preview("transcript", transcript_preview_document(segments))

        duration = max(duration, segments[-1].end)
        progress(68, f"음성 인식 완료 · {len(segments):,}개 구간")
        return Transcript(
            model=self.model_name,
            language=language,
            duration_seconds=duration,
            segments=segments,
        )

    def transcribe_stream(
        self,
        source: "VodAudioSource",
        initial_prompt: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> Transcript:
        """Transcribe bounded PCM chunks while the next audio chunk is streamed.

        Only SOOP's audio-only HLS is read. No complete media or audio file is
        created, and the decoder and batched GPU inference overlap in time.
        """
        from .vod_stream import AudioChunk, iter_audio_chunks

        backend = self._prepare_backend(progress, cancelled)
        batch_size = 8 if backend.runtime.device == "cuda" else 2
        progress(
            5,
            f"{backend.runtime.description} 배치 {batch_size}로 고속 스트림 분석을 시작합니다…",
        )

        work_queue: queue.Queue[object] = queue.Queue(maxsize=2)
        stop_event = threading.Event()

        def should_stop() -> bool:
            return stop_event.is_set() or cancelled()

        def put_item(item: object) -> bool:
            while not stop_event.is_set():
                try:
                    work_queue.put(item, timeout=0.25)
                    return True
                except queue.Full:
                    if cancelled():
                        stop_event.set()
                        return False
            return False

        def produce() -> None:
            try:
                for chunk in iter_audio_chunks(source, should_stop):
                    if not put_item(chunk):
                        return
            except BaseException as error:
                put_item(_StreamFailure(error))
            finally:
                put_item(_STREAM_END)

        producer = threading.Thread(
            target=produce,
            name=f"soop-audio-{source.vod_id}",
            daemon=True,
        )
        producer.start()

        def get_item() -> object:
            while True:
                if cancelled():
                    stop_event.set()
                    raise AnalysisCancelled("분석을 취소했습니다.")
                try:
                    return work_queue.get(timeout=0.25)
                except queue.Empty:
                    if not producer.is_alive() and work_queue.empty():
                        raise RuntimeError("고속 오디오 스트림이 예기치 않게 종료되었습니다.")

        language = "ko"
        accepted: list[tuple[float, float, str]] = []
        lower_boundary = 0.0
        eta = EtaEstimator(source.total_duration_seconds)
        try:
            current_item = get_item()
            if isinstance(current_item, _StreamFailure):
                raise current_item.error
            if current_item is _STREAM_END:
                raise RuntimeError("SOOP 오디오 스트림에 분석할 음성이 없습니다.")

            while True:
                if not isinstance(current_item, AudioChunk):
                    raise RuntimeError("고속 오디오 청크 형식이 올바르지 않습니다.")
                current = current_item
                relative_segments, detected_language = self._transcribe_audio_chunk(
                    backend,
                    current,
                    initial_prompt,
                    batch_size,
                    cancelled,
                )
                language = detected_language or language

                next_item = get_item()
                if isinstance(next_item, _StreamFailure):
                    raise next_item.error
                has_next = next_item is not _STREAM_END
                if has_next and not isinstance(next_item, AudioChunk):
                    raise RuntimeError("고속 오디오 청크 형식이 올바르지 않습니다.")

                upper_boundary = current.end_seconds
                if (
                    has_next
                    and next_item.part_order == current.part_order
                    and next_item.start_seconds < current.end_seconds
                ):
                    upper_boundary = (
                        current.end_seconds + next_item.start_seconds
                    ) / 2.0

                for start, end, text in relative_segments:
                    absolute_start = max(
                        current.start_seconds,
                        min(current.end_seconds, current.start_seconds + start),
                    )
                    absolute_end = max(
                        absolute_start,
                        min(current.end_seconds, current.start_seconds + end),
                    )
                    midpoint = (absolute_start + absolute_end) / 2.0
                    if midpoint + 1e-6 < lower_boundary:
                        continue
                    if has_next and midpoint >= upper_boundary:
                        continue
                    accepted.append((absolute_start, absolute_end, text))

                if preview is not None and accepted:
                    preview(
                        "transcript",
                        transcript_preview_document(accepted),
                    )

                processed_seconds = min(source.total_duration_seconds, upper_boundary)
                ratio = (
                    processed_seconds / source.total_duration_seconds
                    if source.total_duration_seconds > 0
                    else 0.0
                )
                percent = min(68, 5 + int(max(0.0, min(1.0, ratio)) * 63))
                progress(
                    percent,
                    "오디오 스트리밍·배치 인식 중… "
                    f"{format_timestamp(processed_seconds)} / "
                    f"{format_timestamp(source.total_duration_seconds)} · "
                    f"{format_eta(eta.remaining_seconds(processed_seconds))}",
                )

                if not has_next:
                    break
                lower_boundary = (
                    upper_boundary
                    if next_item.start_seconds < current.end_seconds
                    else next_item.start_seconds
                )
                current_item = next_item
        finally:
            stop_event.set()
            producer.join(timeout=2.0)

        segments = [
            TranscriptSegment(
                segment_id=f"s{index:06d}",
                start=start,
                end=end,
                text=text,
            )
            for index, (start, end, text) in enumerate(accepted)
            if text.strip()
        ]
        if not segments:
            raise RuntimeError("SOOP 오디오에서 음성을 인식하지 못했습니다.")

        duration = max(source.total_duration_seconds, segments[-1].end)
        progress(68, f"고속 음성 인식 완료 · {len(segments):,}개 구간")
        return Transcript(
            model=self.model_name,
            language=language,
            duration_seconds=duration,
            segments=segments,
        )

    def transcribe_live(
        self,
        source: "LiveAudioSource",
        initial_prompt: str,
        progress: ProgressCallback,
        stop_requested: CancelCallback,
        preview: PreviewCallback | None = None,
        update: LiveUpdateCallback | None = None,
    ) -> Transcript:
        """Continuously transcribe bounded live audio chunks until stopped."""
        from .live_stream import (
            DEFAULT_LIVE_OVERLAP_SECONDS,
            iter_live_audio_chunks,
        )
        from .vod_stream import AudioChunk

        backend = self._prepare_backend(progress, lambda: False)
        batch_size = 8 if backend.runtime.device == "cuda" else 2
        progress(
            0,
            f"{backend.runtime.description}로 라이브 실시간 인식을 시작합니다…",
        )

        work_queue: queue.Queue[object] = queue.Queue(maxsize=3)
        stop_event = threading.Event()

        def should_stop() -> bool:
            return stop_event.is_set() or stop_requested()

        def put_item(item: object) -> bool:
            while not stop_event.is_set():
                try:
                    work_queue.put(item, timeout=0.25)
                    return True
                except queue.Full:
                    continue
            return False

        def produce() -> None:
            try:
                for chunk in iter_live_audio_chunks(source, should_stop):
                    if not put_item(chunk):
                        return
            except BaseException as error:
                put_item(_StreamFailure(error))
            finally:
                put_item(_STREAM_END)

        producer = threading.Thread(
            target=produce,
            name=f"soop-live-audio-{source.broadcast_no}",
            daemon=True,
        )
        producer.start()

        def get_item() -> object:
            while True:
                try:
                    return work_queue.get(timeout=0.25)
                except queue.Empty:
                    if not producer.is_alive() and work_queue.empty():
                        return _STREAM_END

        language = "ko"
        accepted: list[tuple[float, float, str]] = []
        lower_boundary = source.runtime_seconds
        latest_end = source.runtime_seconds
        started_at = time.monotonic()
        try:
            while True:
                item = get_item()
                if isinstance(item, _StreamFailure):
                    raise item.error
                if item is _STREAM_END:
                    break
                if not isinstance(item, AudioChunk):
                    raise RuntimeError("라이브 오디오 청크 형식이 올바르지 않습니다.")

                relative_segments, detected_language = self._transcribe_audio_chunk(
                    backend,
                    item,
                    initial_prompt,
                    batch_size,
                    lambda: False,
                )
                language = detected_language or language
                for start, end, text in relative_segments:
                    absolute_start = max(
                        item.start_seconds,
                        min(item.end_seconds, item.start_seconds + start),
                    )
                    absolute_end = max(
                        absolute_start,
                        min(item.end_seconds, item.start_seconds + end),
                    )
                    midpoint = (absolute_start + absolute_end) / 2.0
                    if midpoint + 1e-6 < lower_boundary:
                        continue
                    accepted.append((absolute_start, absolute_end, text))

                latest_end = max(latest_end, item.end_seconds)
                lower_boundary = max(
                    lower_boundary,
                    item.end_seconds - DEFAULT_LIVE_OVERLAP_SECONDS / 2.0,
                )
                snapshot = Transcript(
                    model=self.model_name,
                    language=language,
                    duration_seconds=latest_end,
                    segments=[
                        TranscriptSegment(
                            segment_id=f"s{index:06d}",
                            start=start,
                            end=end,
                            text=text,
                        )
                        for index, (start, end, text) in enumerate(accepted)
                        if text.strip()
                    ],
                )
                if preview is not None and snapshot.segments:
                    preview(
                        "live_transcript",
                        transcript_preview_document(snapshot.segments),
                    )
                if update is not None:
                    update(snapshot)

                captured = max(0.0, latest_end - source.runtime_seconds)
                wall_elapsed = max(0.001, time.monotonic() - started_at)
                lag = max(0.0, wall_elapsed - captured)
                lag_text = f" · 처리 지연 약 {format_timestamp(lag)}" if lag >= 5 else ""
                progress(
                    0,
                    "라이브 실시간 음성 인식 중… "
                    f"방송 {format_timestamp(latest_end)} · "
                    f"자막 {len(snapshot.segments):,}개{lag_text}",
                )
        finally:
            stop_event.set()
            producer.join(timeout=9.0)

        segments = [
            TranscriptSegment(
                segment_id=f"s{index:06d}",
                start=start,
                end=end,
                text=text,
            )
            for index, (start, end, text) in enumerate(accepted)
            if text.strip()
        ]
        transcript = Transcript(
            model=self.model_name,
            language=language,
            duration_seconds=latest_end,
            segments=segments,
        )
        if preview is not None and segments:
            preview("live_transcript", transcript_preview_document(segments))
        if update is not None:
            update(transcript)
        progress(
            0,
            f"라이브 수신 종료 · 자막 {len(segments):,}개 · 최종 타임라인 정리 중…",
        )
        return transcript

    def _transcribe_audio_chunk(
        self,
        backend: _WhisperBackend,
        chunk: "AudioChunk",
        initial_prompt: str,
        batch_size: int,
        cancelled: CancelCallback,
    ) -> tuple[list[tuple[float, float, str]], str]:
        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
        audio = chunk.as_float32()
        raw_segments, info = backend.batched_pipeline.transcribe(
            audio,
            language="ko",
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=True,
            initial_prompt=initial_prompt,
            without_timestamps=False,
            batch_size=batch_size,
        )
        segments: list[tuple[float, float, str]] = []
        for raw in raw_segments:
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            text = str(raw.text).strip()
            if text:
                segments.append(
                    (
                        max(0.0, float(raw.start)),
                        max(0.0, float(raw.end)),
                        text,
                    )
                )
        language = str(getattr(info, "language", "ko") or "ko")
        return segments, language


def source_fingerprint(path: str | Path) -> dict[str, object]:
    source = Path(path)
    stat = source.stat()
    return {
        "name": source.name,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def save_transcript_cache(path: str | Path, source_path: str | Path, transcript: Transcript) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source_fingerprint(source_path),
        "transcript": transcript.to_dict(),
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)


def load_transcript_cache(
    path: str | Path,
    source_path: str | Path,
    expected_model: str,
) -> Transcript | None:
    cache_path = Path(path)
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("source") != source_fingerprint(source_path):
            return None
        transcript = Transcript.from_dict(payload["transcript"])
        if transcript.model != expected_model or not transcript.segments:
            return None
        return transcript
    except (OSError, ValueError, TypeError, KeyError):
        return None


def save_vod_transcript_cache(
    path: str | Path,
    vod_id: str,
    source_url: str,
    transcript: Transcript,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": {
            "kind": "soop_vod",
            "vod_id": vod_id,
            "url": source_url,
        },
        "transcript": transcript.to_dict(),
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_vod_transcript_cache(
    path: str | Path,
    vod_id: str,
    source_url: str,
    expected_model: str,
) -> Transcript | None:
    cache_path = Path(path)
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expected_source = {
            "kind": "soop_vod",
            "vod_id": vod_id,
            "url": source_url,
        }
        if payload.get("source") != expected_source:
            return None
        transcript = Transcript.from_dict(payload["transcript"])
        if transcript.model != expected_model or not transcript.segments:
            return None
        return transcript
    except (OSError, ValueError, TypeError, KeyError):
        return None


def format_timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def transcript_preview_document(
    segments: list[TranscriptSegment] | list[tuple[float, float, str]],
) -> str:
    lines: list[str] = []
    for segment in segments:
        if isinstance(segment, TranscriptSegment):
            start = segment.start
            text = segment.text
        else:
            start, _, text = segment
        clean_text = " ".join(str(text).split())
        if clean_text:
            lines.append(f"{format_timestamp(float(start))} {clean_text}")
    return "\n".join(lines) + ("\n" if lines else "")
