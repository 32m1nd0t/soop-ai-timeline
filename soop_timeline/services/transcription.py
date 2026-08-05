from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable, TYPE_CHECKING

from .eta import EtaEstimator, format_eta

if TYPE_CHECKING:
    from .live_stream import LiveAudioSource
    from .vod_stream import AudioChunk, VodAudioSource


ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]
PreviewCallback = Callable[[str, str], None]
CheckpointCallback = Callable[["Transcript"], None]
LiveUpdateCallback = Callable[["LiveTranscriptUpdate"], None]
ReconnectGapCallback = Callable[[float, float], None]


TRANSCRIPT_PIPELINE_VERSION = 2
MAX_TRANSCRIPT_WORD_GAP_SECONDS = 3.0


_NVIDIA_DLL_DIRECTORY_HANDLES: list[object] = []
_NVIDIA_RUNTIME_PATHS_CONFIGURED = False
GPU_ADDON_DIR_ENVIRONMENT = "SOOP_TIMELINE_GPU_RUNTIME_DIR"
GPU_ADDON_DOWNLOAD_URL = (
    "https://github.com/32m1nd0t/soop-ai-timeline/releases/latest/download/"
    "SOOPTimeline-GPU-Addon.exe"
)
GPU_RUNTIME_DLLS = (
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudnn64_9.dll",
)


def gpu_addon_runtime_dir() -> Path:
    override = os.environ.get(GPU_ADDON_DIR_ENVIRONMENT, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(sys.executable).resolve().parent / "gpu-runtime"


def gpu_addon_installed() -> bool:
    directory = gpu_addon_runtime_dir()
    return all((directory / filename).is_file() for filename in GPU_RUNTIME_DLLS)


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
    external_runtime = gpu_addon_runtime_dir()
    if external_runtime.is_dir():
        discovered.append(external_runtime)
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
            "바꾸거나 SOOPTimeline NVIDIA GPU 추가 구성요소를 설치하세요."
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
            "NVIDIA GPU 추가 구성요소를 설치하면 GPU를 사용할 수 있습니다."
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


@dataclass(slots=True, frozen=True)
class TranscriptWord:
    start: float
    end: float
    text: str


@dataclass(slots=True)
class Transcript:
    model: str
    language: str
    duration_seconds: float
    segments: list[TranscriptSegment]
    # Flat, time-ordered word timings used to snap timeline starts to the exact
    # moment a quote is spoken. Empty for transcripts made before this existed.
    words: tuple[TranscriptWord, ...] = ()
    # Audio ranges that were actually inspected. Older VOD caches omitted this
    # field and represented one contiguous prefix ending at duration_seconds.
    covered_ranges: tuple[tuple[float, float], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "segments": [asdict(segment) for segment in self.segments],
            "words": [asdict(word) for word in self.words],
            "covered_ranges": [
                [start, end] for start, end in self.covered_ranges
            ],
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
        raw_words = value.get("words", [])
        words = (
            tuple(
                TranscriptWord(
                    start=float(item["start"]),
                    end=float(item["end"]),
                    text=str(item["text"]),
                )
                for item in raw_words
                if isinstance(item, dict)
            )
            if isinstance(raw_words, list)
            else ()
        )
        raw_covered_ranges = value.get("covered_ranges", [])
        covered_ranges: list[tuple[float, float]] = []
        if isinstance(raw_covered_ranges, list):
            for item in raw_covered_ranges:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    start = max(0.0, float(item[0]))
                    end = max(start, float(item[1]))
                except (TypeError, ValueError):
                    continue
                if end > start:
                    covered_ranges.append((start, end))
        return cls(
            model=str(value.get("model", "")),
            language=str(value.get("language", "ko")),
            duration_seconds=float(value.get("duration_seconds", 0.0)),
            segments=segments,
            words=words,
            covered_ranges=tuple(covered_ranges),
        )


def merge_covered_ranges(
    ranges: Iterable[tuple[float, float]],
    *,
    total_duration: float | None = None,
    join_tolerance: float = 0.25,
) -> tuple[tuple[float, float], ...]:
    """Normalize, clip, and union audio coverage ranges."""

    limit = (
        max(0.0, float(total_duration))
        if total_duration is not None
        else None
    )
    normalized: list[tuple[float, float]] = []
    for raw_start, raw_end in ranges:
        try:
            start = max(0.0, float(raw_start))
            end = max(0.0, float(raw_end))
        except (TypeError, ValueError):
            continue
        if limit is not None:
            start = min(limit, start)
            end = min(limit, end)
        if end - start <= 1e-6:
            continue
        normalized.append((start, end))
    normalized.sort()

    merged: list[tuple[float, float]] = []
    tolerance = max(0.0, float(join_tolerance))
    for start, end in normalized:
        if not merged or start > merged[-1][1] + tolerance:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def missing_covered_ranges(
    total_duration: float,
    covered_ranges: Iterable[tuple[float, float]],
    *,
    minimum_seconds: float = 0.5,
) -> tuple[tuple[float, float], ...]:
    """Return only the portions of a VOD that still require speech recognition."""

    total = max(0.0, float(total_duration))
    covered = merge_covered_ranges(
        covered_ranges,
        total_duration=total,
    )
    missing: list[tuple[float, float]] = []
    cursor = 0.0
    minimum = max(0.0, float(minimum_seconds))
    for start, end in covered:
        if start - cursor >= minimum:
            missing.append((cursor, start))
        cursor = max(cursor, end)
    if total - cursor >= minimum:
        missing.append((cursor, total))
    return tuple(missing)


def covered_duration(ranges: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in merge_covered_ranges(ranges))


def timestamp_in_ranges(
    seconds: float,
    ranges: Iterable[tuple[float, float]],
) -> bool:
    value = float(seconds)
    return any(start - 1e-6 <= value <= end + 1e-6 for start, end in ranges)


@dataclass(slots=True, frozen=True)
class LiveTranscriptUpdate:
    """Only the text newly accepted from one live audio chunk.

    Keeping live updates incremental avoids copying the complete multi-hour
    transcript after every 15-second chunk. The final ``Transcript`` is still
    returned once when reception ends.
    """

    model: str
    language: str
    duration_seconds: float
    segments: tuple[TranscriptSegment, ...] = ()
    words: tuple[TranscriptWord, ...] = ()


def _extract_words(raw_segment: object) -> list[TranscriptWord]:
    """Read faster-whisper per-word timings from a raw segment (times as-is)."""
    raw_words = getattr(raw_segment, "words", None) or []
    result: list[TranscriptWord] = []
    for word in raw_words:
        text = str(getattr(word, "word", "") or "").strip()
        if not text:
            continue
        start = max(0.0, float(getattr(word, "start", 0.0) or 0.0))
        end = max(start, float(getattr(word, "end", 0.0) or 0.0))
        result.append(TranscriptWord(start, end, text))
    return result


def _join_transcript_words(words: Iterable[TranscriptWord]) -> str:
    text = " ".join(word.text.strip() for word in words if word.text.strip())
    text = re.sub(r"\s+([,.!?;:%…~\)\]\}〉》」』】”’])", r"\1", text)
    text = re.sub(r"([\(\[\{〈《「『【“‘])\s+", r"\1", text)
    return " ".join(text.split())


def _is_probable_broken_transcript_noise(text: str) -> bool:
    """Drop only obviously corrupted repetitive output, not ordinary reactions."""

    compact = "".join(text.split())
    if "\ufffd" not in compact:
        return False
    spoken = [character.casefold() for character in compact if character.isalnum()]
    if len(spoken) < 12:
        return False
    dominant = max(spoken.count(character) for character in set(spoken))
    return dominant / len(spoken) >= 0.7


def split_transcript_segment_by_word_gaps(
    start: float,
    end: float,
    text: str,
    words: Iterable[TranscriptWord],
    *,
    max_gap_seconds: float = MAX_TRANSCRIPT_WORD_GAP_SECONDS,
) -> list[tuple[float, float, str, list[TranscriptWord]]]:
    """Split one Whisper segment when VAD joined distant speech islands.

    Batched faster-whisper can return one segment whose words are separated by
    minutes after VAD restores original timestamps. Segment-level consumers then
    assign every sentence to the first island. Per-word timings are the reliable
    boundary evidence, so split only when they expose a real gap.
    """

    clean_text = " ".join(str(text).split())
    if not clean_text or _is_probable_broken_transcript_noise(clean_text):
        return []
    ordered_words = sorted(
        (word for word in words if word.text.strip()),
        key=lambda word: (word.start, word.end, word.text),
    )
    if not ordered_words:
        normalized_start = max(0.0, start)
        return [
            (
                normalized_start,
                max(normalized_start, end),
                clean_text,
                [],
            )
        ]

    groups: list[list[TranscriptWord]] = []
    current: list[TranscriptWord] = []
    maximum_gap = max(0.0, float(max_gap_seconds))
    for word in ordered_words:
        if current and word.start - current[-1].end > maximum_gap:
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)

    if len(groups) == 1:
        normalized_start = max(0.0, start)
        return [
            (
                normalized_start,
                max(normalized_start, end),
                clean_text,
                ordered_words,
            )
        ]

    split_segments: list[tuple[float, float, str, list[TranscriptWord]]] = []
    for group in groups:
        group_text = _join_transcript_words(group)
        if not group_text or _is_probable_broken_transcript_noise(group_text):
            continue
        group_start = max(0.0, group[0].start)
        group_end = max(group_start, group[-1].end)
        split_segments.append((group_start, group_end, group_text, group))
    return split_segments


@dataclass(slots=True)
class _WhisperBackend:
    runtime: WhisperRuntime
    model: object
    batched_pipeline: object


@dataclass(slots=True, frozen=True)
class _StreamFailure:
    error: BaseException


@dataclass(slots=True, frozen=True)
class _RangedAudioChunk:
    chunk: object
    range_index: int
    accept_start: float
    accept_end: float


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
        # Waiting behind another first-time model load used to be completely
        # uninterruptible.  Polling the lock lets queued jobs honour cancellation
        # immediately; the native model constructor itself cannot be interrupted,
        # so we also check once more as soon as it returns.
        while not _MODEL_LOCK.acquire(timeout=0.1):
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
        try:
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
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
        finally:
            _MODEL_LOCK.release()

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
            word_timestamps=True,
        )

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        language = str(getattr(info, "language", "ko") or "ko")
        segments: list[TranscriptSegment] = []
        words: list[TranscriptWord] = []
        last_percent = -1
        last_preview_at = 0.0
        eta = EtaEstimator(duration)
        for raw in raw_segments:
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            text = str(raw.text).strip()
            if text:
                split_segments = split_transcript_segment_by_word_gaps(
                    float(raw.start),
                    float(raw.end),
                    text,
                    _extract_words(raw),
                )
                for (
                    split_start,
                    split_end,
                    split_text,
                    split_words,
                ) in split_segments:
                    segments.append(
                        TranscriptSegment(
                            segment_id=f"s{len(segments):06d}",
                            start=split_start,
                            end=split_end,
                            text=split_text,
                        )
                    )
                    words.extend(split_words)
                now = time.monotonic()
                if split_segments and preview is not None and (
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

        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
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
            words=tuple(words),
        )

    def transcribe_stream(
        self,
        source: "VodAudioSource",
        initial_prompt: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
        resume: Transcript | None = None,
        checkpoint: CheckpointCallback | None = None,
        reusable: Transcript | None = None,
        reusable_ranges: Iterable[tuple[float, float]] = (),
    ) -> Transcript:
        """Transcribe bounded PCM chunks while the next audio chunk is streamed.

        Only SOOP's audio-only HLS is read. No complete media or audio file is
        created, and the decoder and batched GPU inference overlap in time.
        Validated live-caption ranges can be reused so Whisper only receives
        genuinely missing portions of the completed replay.
        """
        from .vod_stream import (
            DEFAULT_OVERLAP_SECONDS,
            AudioChunk,
            iter_audio_chunks,
        )

        total_duration = max(0.0, float(source.total_duration_seconds))
        resume_segments = list(resume.segments) if resume is not None else []
        resume_boundary = (
            max(
                float(getattr(resume, "duration_seconds", 0.0) or 0.0),
                max((segment.end for segment in resume_segments), default=0.0),
            )
            if resume_segments
            else 0.0
        )
        resume_coverage = merge_covered_ranges(
            (
                resume.covered_ranges
                if resume is not None and resume.covered_ranges
                else ((0.0, resume_boundary),) if resume_segments else ()
            ),
            total_duration=total_duration,
        )
        reusable_coverage = merge_covered_ranges(
            reusable_ranges if reusable is not None else (),
            total_duration=total_duration,
        )
        initial_coverage = merge_covered_ranges(
            (*resume_coverage, *reusable_coverage),
            total_duration=total_duration,
        )
        missing_ranges = missing_covered_ranges(
            total_duration,
            initial_coverage,
        )

        accepted: list[tuple[float, float, str]] = [
            (segment.start, segment.end, segment.text)
            for segment in resume_segments
            if segment.text.strip()
        ]
        accepted_words: list[TranscriptWord] = (
            list(resume.words) if resume is not None else []
        )
        if reusable is not None:
            for segment in reusable.segments:
                midpoint = (segment.start + segment.end) / 2.0
                if (
                    segment.text.strip()
                    and timestamp_in_ranges(midpoint, reusable_coverage)
                    and not timestamp_in_ranges(midpoint, resume_coverage)
                ):
                    accepted.append((segment.start, segment.end, segment.text))
            for word in reusable.words:
                midpoint = (word.start + word.end) / 2.0
                if (
                    timestamp_in_ranges(midpoint, reusable_coverage)
                    and not timestamp_in_ranges(midpoint, resume_coverage)
                ):
                    accepted_words.append(word)

        def materialized_segments() -> list[TranscriptSegment]:
            ordered = sorted(accepted, key=lambda item: (item[0], item[1], item[2]))
            result: list[TranscriptSegment] = []
            seen: set[tuple[int, int, str]] = set()
            for start, end, text in ordered:
                normalized = " ".join(text.split())
                if not normalized:
                    continue
                key = (
                    round(start * 1_000),
                    round(end * 1_000),
                    normalized.casefold(),
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(
                    TranscriptSegment(
                        segment_id=f"s{len(result):06d}",
                        start=start,
                        end=end,
                        text=text,
                    )
                )
            return result

        def materialized_words() -> tuple[TranscriptWord, ...]:
            ordered = sorted(
                accepted_words,
                key=lambda word: (word.start, word.end, word.text),
            )
            result: list[TranscriptWord] = []
            seen: set[tuple[int, int, str]] = set()
            for word in ordered:
                key = (
                    round(word.start * 1_000),
                    round(word.end * 1_000),
                    word.text,
                )
                if key in seen:
                    continue
                seen.add(key)
                result.append(word)
            return tuple(result)

        if resume_segments:
            progress(
                5,
                f"저장된 중간 자막 {len(resume_segments):,}개를 이어서 분석합니다 · "
                f"확인 완료 {format_timestamp(covered_duration(resume_coverage))}",
            )
        if reusable is not None and reusable_coverage:
            progress(
                5,
                f"같은 방송의 라이브 자막 {len(reusable.segments):,}개를 재사용합니다 · "
                f"중복 제외 {format_timestamp(covered_duration(reusable_coverage))}",
            )
        initial_segments = materialized_segments()
        if preview is not None and initial_segments:
            preview("transcript", transcript_preview_document(initial_segments))

        if not missing_ranges:
            if not initial_segments:
                raise RuntimeError("재사용할 수 있는 자막 구간을 찾지 못했습니다.")
            progress(
                68,
                "전체 다시보기 구간이 기존 자막으로 채워져 Whisper 중복 분석을 "
                f"건너뜁니다 · 자막 {len(initial_segments):,}개",
            )
            return Transcript(
                model=self.model_name,
                language=(
                    (resume.language if resume is not None else "")
                    or (reusable.language if reusable is not None else "")
                    or "ko"
                ),
                duration_seconds=total_duration,
                segments=initial_segments,
                words=materialized_words(),
                covered_ranges=((0.0, total_duration),),
            )

        backend = self._prepare_backend(progress, cancelled)
        batch_size = 8 if backend.runtime.device == "cuda" else 2
        missing_duration = covered_duration(missing_ranges)
        progress(
            5,
            f"{backend.runtime.description} 배치 {batch_size} · "
            f"기존 자막과 겹치지 않는 {format_timestamp(missing_duration)}만 "
            "고속 분석합니다…",
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
                for range_index, (range_start, range_end) in enumerate(
                    missing_ranges
                ):
                    emitted = False
                    context_start = max(
                        0.0,
                        range_start - DEFAULT_OVERLAP_SECONDS,
                    )
                    context_end = min(
                        total_duration,
                        range_end + DEFAULT_OVERLAP_SECONDS,
                    )
                    for chunk in iter_audio_chunks(
                        source,
                        should_stop,
                        start_seconds=context_start,
                        end_seconds=context_end,
                    ):
                        emitted = True
                        if not put_item(
                            _RangedAudioChunk(
                                chunk=chunk,
                                range_index=range_index,
                                accept_start=range_start,
                                accept_end=range_end,
                            )
                        ):
                            return
                    if not emitted and not should_stop():
                        raise RuntimeError(
                            "누락 구간의 오디오를 읽지 못했습니다: "
                            f"{format_timestamp(range_start)}~"
                            f"{format_timestamp(range_end)}"
                        )
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

        language = (
            (resume.language if resume is not None else "")
            or (reusable.language if reusable is not None else "")
            or "ko"
        )
        completed_range_ends = [start for start, _end in missing_ranges]
        lower_boundary = missing_ranges[0][0]
        eta = EtaEstimator(missing_duration)
        try:
            current_item = get_item()
            if isinstance(current_item, _StreamFailure):
                raise current_item.error
            if current_item is _STREAM_END:
                raise RuntimeError("SOOP 오디오 스트림에 분석할 음성이 없습니다.")

            while True:
                if not isinstance(current_item, _RangedAudioChunk) or not isinstance(
                    current_item.chunk,
                    AudioChunk,
                ):
                    raise RuntimeError("고속 오디오 청크 형식이 올바르지 않습니다.")
                current_wrapper = current_item
                current = current_wrapper.chunk
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
                if has_next and (
                    not isinstance(next_item, _RangedAudioChunk)
                    or not isinstance(next_item.chunk, AudioChunk)
                ):
                    raise RuntimeError("고속 오디오 청크 형식이 올바르지 않습니다.")

                upper_boundary = current.end_seconds
                if (
                    has_next
                    and next_item.range_index == current_wrapper.range_index
                    and next_item.chunk.part_order == current.part_order
                    and next_item.chunk.start_seconds < current.end_seconds
                ):
                    upper_boundary = (
                        current.end_seconds + next_item.chunk.start_seconds
                    ) / 2.0
                accept_lower = max(
                    current_wrapper.accept_start,
                    lower_boundary,
                )
                accept_upper = min(
                    current_wrapper.accept_end,
                    upper_boundary,
                )

                for start, end, text, rel_words in relative_segments:
                    absolute_start = max(
                        current.start_seconds,
                        min(current.end_seconds, current.start_seconds + start),
                    )
                    absolute_end = max(
                        absolute_start,
                        min(current.end_seconds, current.start_seconds + end),
                    )
                    midpoint = (absolute_start + absolute_end) / 2.0
                    if midpoint + 1e-6 < accept_lower:
                        continue
                    if midpoint >= accept_upper:
                        continue
                    accepted.append((absolute_start, absolute_end, text))
                    for word in rel_words:
                        word_start = max(
                            current.start_seconds,
                            min(current.end_seconds, current.start_seconds + word.start),
                        )
                        word_end = max(
                            word_start,
                            min(current.end_seconds, current.start_seconds + word.end),
                        )
                        word_midpoint = (word_start + word_end) / 2.0
                        if accept_lower <= word_midpoint < accept_upper:
                            accepted_words.append(
                                TranscriptWord(word_start, word_end, word.text)
                            )

                if preview is not None and accepted:
                    preview_segments = materialized_segments()
                    preview(
                        "transcript",
                        transcript_preview_document(preview_segments),
                    )

                completed_range_ends[current_wrapper.range_index] = max(
                    completed_range_ends[current_wrapper.range_index],
                    min(
                        current_wrapper.accept_end,
                        max(current_wrapper.accept_start, upper_boundary),
                    ),
                )
                checkpoint_coverage = merge_covered_ranges(
                    (
                        *initial_coverage,
                        *(
                            (start, completed_range_ends[index])
                            for index, (start, _end) in enumerate(missing_ranges)
                            if completed_range_ends[index] > start
                        ),
                    ),
                    total_duration=total_duration,
                )
                if checkpoint is not None and accepted:
                    checkpoint_segments = materialized_segments()
                    checkpoint(
                        Transcript(
                            model=self.model_name,
                            language=language,
                            duration_seconds=max(
                                (end for _start, end in checkpoint_coverage),
                                default=0.0,
                            ),
                            segments=checkpoint_segments,
                            words=materialized_words(),
                            covered_ranges=checkpoint_coverage,
                        )
                    )

                processed_missing = sum(
                    max(0.0, completed_range_ends[index] - start)
                    for index, (start, _end) in enumerate(missing_ranges)
                )
                ratio = (
                    processed_missing / missing_duration
                    if missing_duration > 0
                    else 0.0
                )
                percent = min(68, 5 + int(max(0.0, min(1.0, ratio)) * 63))
                progress(
                    percent,
                    "중복 제외 오디오 인식 중… 새 분석 "
                    f"{format_timestamp(processed_missing)} / "
                    f"{format_timestamp(missing_duration)} · 전체 위치 "
                    f"{format_timestamp(min(total_duration, upper_boundary))} · "
                    f"{format_eta(eta.remaining_seconds(processed_missing))}",
                )

                if not has_next:
                    break
                if next_item.range_index != current_wrapper.range_index:
                    lower_boundary = next_item.accept_start
                else:
                    lower_boundary = (
                        upper_boundary
                        if next_item.chunk.start_seconds < current.end_seconds
                        else max(
                            next_item.accept_start,
                            next_item.chunk.start_seconds,
                        )
                    )
                current_item = next_item
        finally:
            stop_event.set()
            producer.join(timeout=2.0)

        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
        segments = materialized_segments()
        if not segments:
            raise RuntimeError("SOOP 오디오에서 음성을 인식하지 못했습니다.")

        duration = max(total_duration, segments[-1].end)
        progress(
            68,
            f"고속 음성 인식 완료 · 기존 구간 재사용 + 누락 구간 분석 · "
            f"자막 {len(segments):,}개",
        )
        return Transcript(
            model=self.model_name,
            language=language,
            duration_seconds=duration,
            segments=segments,
            words=materialized_words(),
            covered_ranges=((0.0, total_duration),),
        )

    def transcribe_live(
        self,
        source: "LiveAudioSource",
        initial_prompt: str,
        progress: ProgressCallback,
        stop_requested: CancelCallback,
        preview: PreviewCallback | None = None,
        update: LiveUpdateCallback | None = None,
        reconnect_gap: ReconnectGapCallback | None = None,
    ) -> Transcript:
        """Continuously transcribe bounded live audio chunks until stopped."""
        from .live_stream import (
            DEFAULT_LIVE_OVERLAP_SECONDS,
            iter_live_audio_chunks,
        )
        from .vod_stream import AudioChunk

        # Loading a large Whisper model can itself take noticeable time.  Pass
        # the real stop callback so closing the app during startup does not
        # have to wait for the whole model preparation path to finish.
        backend = self._prepare_backend(progress, stop_requested)
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
                for chunk in iter_live_audio_chunks(
                    source,
                    should_stop,
                    reconnect_gap=reconnect_gap,
                ):
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
        accepted: list[TranscriptSegment] = []
        accepted_words: list[TranscriptWord] = []
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
                new_segments: list[TranscriptSegment] = []
                new_words: list[TranscriptWord] = []
                for start, end, text, rel_words in relative_segments:
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
                    clean_text = text.strip()
                    if not clean_text:
                        continue
                    candidate = TranscriptSegment(
                        segment_id=f"s{len(accepted):06d}",
                        start=absolute_start,
                        end=absolute_end,
                        text=clean_text,
                    )
                    if _is_duplicate_live_segment(
                        candidate,
                        accepted,
                        DEFAULT_LIVE_OVERLAP_SECONDS,
                    ):
                        continue
                    accepted.append(candidate)
                    new_segments.append(candidate)
                    for word in rel_words:
                        word_start = max(
                            item.start_seconds,
                            min(item.end_seconds, item.start_seconds + word.start),
                        )
                        word_end = max(
                            word_start,
                            min(item.end_seconds, item.start_seconds + word.end),
                        )
                        accepted_word = TranscriptWord(word_start, word_end, word.text)
                        accepted_words.append(accepted_word)
                        new_words.append(accepted_word)

                latest_end = max(latest_end, item.end_seconds)
                lower_boundary = max(
                    lower_boundary,
                    item.end_seconds - DEFAULT_LIVE_OVERLAP_SECONDS / 2.0,
                )
                incremental = LiveTranscriptUpdate(
                    model=self.model_name,
                    language=language,
                    duration_seconds=latest_end,
                    segments=tuple(new_segments),
                    words=tuple(new_words),
                )
                if preview is not None and new_segments:
                    preview(
                        "live_transcript_append",
                        transcript_preview_document(new_segments),
                    )
                if update is not None:
                    update(incremental)

                captured = max(0.0, latest_end - source.runtime_seconds)
                wall_elapsed = max(0.001, time.monotonic() - started_at)
                lag = max(0.0, wall_elapsed - captured)
                lag_text = f" · 처리 지연 약 {format_timestamp(lag)}" if lag >= 5 else ""
                progress(
                    0,
                    "라이브 실시간 음성 인식 중… "
                    f"방송 {format_timestamp(latest_end)} · "
                    f"자막 {len(accepted):,}개{lag_text}",
                )
        finally:
            stop_event.set()
            producer.join(timeout=9.0)

        segments = list(accepted)
        transcript = Transcript(
            model=self.model_name,
            language=language,
            duration_seconds=latest_end,
            segments=segments,
            words=tuple(accepted_words),
        )
        progress(
            0,
            f"라이브 수신 종료 · 자막 {len(segments):,}개 · 누적 자막 저장 중…",
        )
        return transcript

    def _transcribe_audio_chunk(
        self,
        backend: _WhisperBackend,
        chunk: "AudioChunk",
        initial_prompt: str,
        batch_size: int,
        cancelled: CancelCallback,
    ) -> tuple[list[tuple[float, float, str, list[TranscriptWord]]], str]:
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
            word_timestamps=True,
        )
        segments: list[tuple[float, float, str, list[TranscriptWord]]] = []
        for raw in raw_segments:
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            text = str(raw.text).strip()
            if text:
                segments.extend(
                    split_transcript_segment_by_word_gaps(
                        float(raw.start),
                        float(raw.end),
                        text,
                        _extract_words(raw),
                    )
                )
        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
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


def vod_source_fingerprint(source: "VodAudioSource") -> dict[str, object]:
    """Return stable metadata that changes when the published VOD grows/changes.

    Signed HLS URLs may rotate even when the media is identical, so the
    fingerprint deliberately uses SOOP's declared durations and part ordering
    instead of the temporary playlist URLs.
    """

    return {
        "total_duration_ms": round(max(0.0, source.total_duration_seconds) * 1_000),
        "parts": [
            {
                "order": int(part.order),
                "duration_ms": round(max(0.0, part.duration_seconds) * 1_000),
            }
            for part in source.parts
        ],
    }


def _transcript_covers_vod_source(
    transcript: Transcript,
    source: "VodAudioSource",
) -> bool:
    total = max(0.0, float(source.total_duration_seconds))
    if total <= 0:
        return False
    boundary = max(
        float(transcript.duration_seconds),
        max((segment.end for segment in transcript.segments), default=0.0),
    )
    coverage = transcript.covered_ranges or ((0.0, boundary),)
    # SOOP metadata and decoded media can differ by a fraction of a second.
    # Never allow that tolerance to hide a meaningful uninspected section.
    tolerance = max(1.0, min(5.0, total * 0.0005))
    return not missing_covered_ranges(
        total,
        coverage,
        minimum_seconds=tolerance,
    )


def save_transcript_cache(path: str | Path, source_path: str | Path, transcript: Transcript) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pipeline_version": TRANSCRIPT_PIPELINE_VERSION,
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
        if payload.get("pipeline_version") != TRANSCRIPT_PIPELINE_VERSION:
            return None
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
    *,
    vod_source: "VodAudioSource | None" = None,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source: dict[str, object] = {
        "kind": "soop_vod",
        "vod_id": vod_id,
        "url": source_url,
    }
    if vod_source is not None:
        source["media"] = vod_source_fingerprint(vod_source)
    payload = {
        "pipeline_version": TRANSCRIPT_PIPELINE_VERSION,
        "source": source,
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
    *,
    vod_source: "VodAudioSource | None" = None,
    require_complete: bool = False,
) -> Transcript | None:
    cache_path = Path(path)
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("pipeline_version") != TRANSCRIPT_PIPELINE_VERSION:
            return None
        cached_source = payload.get("source")
        if not isinstance(cached_source, dict) or any(
            cached_source.get(key) != expected
            for key, expected in (
                ("kind", "soop_vod"),
                ("vod_id", vod_id),
                ("url", source_url),
            )
        ):
            return None
        if vod_source is not None:
            cached_media = cached_source.get("media")
            if (
                cached_media is not None
                and cached_media != vod_source_fingerprint(vod_source)
            ):
                return None
        transcript = Transcript.from_dict(payload["transcript"])
        if transcript.model != expected_model or not transcript.segments:
            return None
        if (
            vod_source is not None
            and require_complete
            and not _transcript_covers_vod_source(transcript, vod_source)
        ):
            return None
        return transcript
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _normalized_spoken_text(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _is_duplicate_live_segment(
    candidate: TranscriptSegment,
    accepted: list[TranscriptSegment],
    overlap_seconds: float,
) -> bool:
    """Reject the same utterance recognized again in a live overlap window."""
    normalized = _normalized_spoken_text(candidate.text)
    if not normalized:
        return False
    time_tolerance = max(1.0, float(overlap_seconds)) + 1.5
    for previous in reversed(accepted[-12:]):
        if candidate.start - previous.end > time_tolerance:
            break
        if abs(candidate.start - previous.start) > time_tolerance:
            continue
        prior = _normalized_spoken_text(previous.text)
        if not prior:
            continue
        if normalized == prior:
            return True
        if min(len(normalized), len(prior)) >= 6:
            similarity = SequenceMatcher(
                None,
                normalized,
                prior,
                autojunk=False,
            ).ratio()
            if similarity >= 0.88:
                return True
    return False


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
