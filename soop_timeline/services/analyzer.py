from __future__ import annotations

from abc import ABC, abstractmethod
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import queue
import threading
from typing import Callable, Iterable

from ..models import Vod
from ..paths import analysis_data_dir
from .ai_provider import GEMINI_PROVIDER, create_ai_provider
from .credentials import get_gemini_api_key
from .gemini_timeline import (
    AITimelineGenerator,
    DEFAULT_TOPIC_GRANULARITY,
    GeneratedTimeline,
    GeminiTimelineGenerator,
    TimelineGenerationState,
    TimelineEntry,
    build_overall_summary,
    deduplicate_entries,
)
from .preferences import LIVE_AI_MODE_SETTING, live_ai_mode
from .gemini_style import parse_timeline_document
from .timeline_timestamp import parse_timestamp
from .timeline_document import initial_timeline_document
from .transcription import (
    AnalysisCancelled,
    CancelCallback,
    covered_duration,
    FasterWhisperTranscriber,
    LiveTranscriptUpdate,
    merge_covered_ranges,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
    format_timestamp,
    PreviewCallback,
    ProgressCallback,
    load_transcript_cache,
    load_vod_transcript_cache,
    save_transcript_cache,
    save_vod_transcript_cache,
    transcript_preview_document,
)


DEFAULT_GEMINI_MODEL = "gemini-flash-lite-latest"
DEFAULT_WHISPER_MODEL = "large-v3-turbo"
LIVE_SUMMARY_OVERLAP_SECONDS = 30
LIVE_TOPIC_CONFIRMATION_SECONDS = 30
TIMELINE_CHECKPOINT_FILENAME = "timeline.partial.json"
LIVE_TRANSCRIPT_FILENAME = "live-transcript.json"
LIVE_TRANSCRIPT_JOURNAL_FILENAME = "live-transcript.jsonl"
LIVE_RECONNECT_LOG_FILENAME = "live-reconnect.jsonl"
LIVE_ANALYSIS_STATE_FILENAME = "live-analysis-state.json"


@dataclass(slots=True, frozen=True)
class AnalyzerConfig:
    gemini_model: str = DEFAULT_GEMINI_MODEL
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_device: str = "auto"
    gemini_api_key: str = ""
    topic_granularity: str = DEFAULT_TOPIC_GRANULARITY
    live_ai_mode: str = "saving"


@dataclass(slots=True, frozen=True)
class LiveReplayTranscriptReuse:
    transcript: Transcript
    covered_ranges: tuple[tuple[float, float], ...]
    session_count: int


class TimelineAnalyzer(ABC):
    """Boundary for a local-STT and cloud topic-summary pipeline."""

    @property
    @abstractmethod
    def available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def initial_document(self, vod: Vod) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def unavailable_reason(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_media(
        self,
        vod: Vod,
        media_path: str | Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> str:
        raise NotImplementedError

    @abstractmethod
    def analyze_vod(
        self,
        vod: Vod,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
        reusable_live_vods: Iterable[Vod] = (),
    ) -> str:
        raise NotImplementedError


class ReviewDraftAnalyzer(TimelineAnalyzer):
    """Creates the review document until the real audio analyzer is connected."""

    @property
    def available(self) -> bool:
        return False

    def initial_document(self, vod: Vod) -> str:
        return initial_timeline_document(vod.title)

    @property
    def unavailable_reason(self) -> str:
        return "분석기가 설정되지 않았습니다."

    def analyze_media(
        self,
        vod: Vod,
        media_path: str | Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> str:
        del vod, media_path, progress, cancelled, preview
        raise RuntimeError(self.unavailable_reason)

    def analyze_vod(
        self,
        vod: Vod,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
        reusable_live_vods: Iterable[Vod] = (),
    ) -> str:
        del vod, progress, cancelled, preview, reusable_live_vods
        raise RuntimeError(self.unavailable_reason)


class LocalWhisperGeminiAnalyzer(TimelineAnalyzer):
    def __init__(
        self,
        config: AnalyzerConfig,
        transcriber_factory: Callable[..., FasterWhisperTranscriber] | None = None,
        generator_factory: Callable[[str, str], AITimelineGenerator] | None = None,
    ):
        self.config = config
        self._transcriber_factory = transcriber_factory or (
            lambda model, device: FasterWhisperTranscriber(
                model_name=model,
                device=device,
            )
        )
        self._generator_factory = generator_factory or (
            lambda key, model: GeminiTimelineGenerator(
                key,
                model,
                topic_granularity=config.topic_granularity,
            )
        )
        self.last_usage_summary = ""
        self.last_result_warning = ""

    @classmethod
    def from_database(cls, database: object) -> "LocalWhisperGeminiAnalyzer":
        return cls(
            AnalyzerConfig(
                gemini_model=database.get_setting(
                    "gemini_model",
                    DEFAULT_GEMINI_MODEL,
                ),
                whisper_model=database.get_setting("whisper_model", DEFAULT_WHISPER_MODEL),
                whisper_device=database.get_setting("whisper_device", "auto"),
                gemini_api_key=get_gemini_api_key(),
                topic_granularity=database.get_setting(
                    "topic_granularity",
                    DEFAULT_TOPIC_GRANULARITY,
                ),
                live_ai_mode=database.get_setting(LIVE_AI_MODE_SETTING, "saving"),
            )
        )

    @property
    def available(self) -> bool:
        provider = create_ai_provider(
            GEMINI_PROVIDER,
            self.config.gemini_api_key,
            self.config.gemini_model,
        )
        if not provider.available:
            return False
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        provider = create_ai_provider(
            GEMINI_PROVIDER,
            self.config.gemini_api_key,
            self.config.gemini_model,
        )
        if not provider.available:
            return provider.unavailable_reason
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "faster-whisper가 설치되지 않았습니다."
        return ""

    @property
    def provider_name(self) -> str:
        return "Gemini"

    def _new_generator(self) -> AITimelineGenerator:
        return self._generator_factory(
            self.config.gemini_api_key,
            self.config.gemini_model,
        )

    def _preflight(
        self,
        generator: object,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> None:
        test_connection = getattr(generator, "test_connection", None)
        if not callable(test_connection):
            return
        progress(1, f"{self.provider_name} API 연결과 모델 권한을 먼저 확인합니다…")
        message = str(test_connection(cancelled))
        progress(2, message)

    def _capture_usage(self, generator: object) -> None:
        summary = getattr(generator, "usage_summary", None)
        self.last_usage_summary = str(summary()) if callable(summary) else ""

    def initial_document(self, vod: Vod) -> str:
        return initial_timeline_document(vod.title)

    def _generate_with_checkpoint(
        self,
        generator: AITimelineGenerator,
        vod: Vod,
        transcript: Transcript,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None,
        *,
        granularity: str,
    ) -> GeneratedTimeline:
        checkpoint_path = analysis_data_dir(vod.vod_id) / TIMELINE_CHECKPOINT_FILENAME
        checkpoint_key = timeline_checkpoint_key(
            vod,
            transcript,
            self.config.gemini_model,
            granularity,
        )
        resume = load_timeline_generation_state(checkpoint_path, checkpoint_key)
        if resume is None:
            checkpoint_path.unlink(missing_ok=True)
        timeline = generator.generate(
            vod,
            transcript,
            progress,
            cancelled,
            preview=preview,
            checkpoint_key=checkpoint_key,
            resume_state=resume,
            checkpoint=lambda state: save_timeline_generation_state(
                checkpoint_path,
                state,
            ),
        )
        self.last_result_warning = str(getattr(generator, "last_warning", "") or "")
        if not self.last_result_warning:
            checkpoint_path.unlink(missing_ok=True)
        return timeline

    def analyze_media(
        self,
        vod: Vod,
        media_path: str | Path,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)

        generator = self._new_generator()
        self._preflight(generator, progress, cancelled)

        source_path = Path(media_path)
        cache_path = analysis_data_dir(vod.vod_id) / "transcript.json"
        transcript = load_transcript_cache(
            cache_path,
            source_path,
            self.config.whisper_model,
        )
        if transcript is None:
            transcriber = self._transcriber_factory(
                self.config.whisper_model,
                self.config.whisper_device,
            )
            prompt = build_whisper_prompt(vod, live=False)
            transcript = transcriber.transcribe(
                source_path,
                initial_prompt=prompt,
                progress=progress,
                cancelled=cancelled,
                preview=preview,
            )
            save_transcript_cache(cache_path, source_path, transcript)
        else:
            progress(68, f"저장된 자막 {len(transcript.segments):,}개 구간을 재사용합니다.")
            if preview is not None:
                preview("transcript", transcript_preview_document(transcript.segments))

        timeline = self._generate_with_checkpoint(
            generator,
            vod,
            transcript,
            progress,
            cancelled,
            preview,
            granularity=self.config.topic_granularity,
        )
        self._capture_usage(generator)
        suffix = f" · {self.last_usage_summary}" if self.last_usage_summary else ""
        if self.last_result_warning:
            progress(100, f"{self.last_result_warning}{suffix}")
        else:
            progress(100, f"AI 타임라인 생성이 완료되었습니다{suffix}.")
        return timeline.to_document()

    def regroup_vod(
        self,
        vod: Vod,
        topic_granularity: str,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> str:
        """Regenerate topic boundaries from the cached transcript only."""
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        transcript = load_cached_transcript(vod)
        if transcript is None:
            raise RuntimeError(
                "복구할 로컬 자막이 없습니다. 먼저 영상 또는 라이브 AI 분석을 시작하세요."
            )
        generator = GeminiTimelineGenerator(
            self.config.gemini_api_key,
            self.config.gemini_model,
            topic_granularity=topic_granularity,
        )
        self._preflight(generator, progress, cancelled)
        progress(
            5,
            f"저장된 자막 {len(transcript.segments):,}개로 주제 경계를 다시 판정합니다 · "
            "Whisper는 실행하지 않습니다.",
        )
        timeline = self._generate_with_checkpoint(
            generator,
            vod,
            transcript,
            progress,
            cancelled,
            preview,
            granularity=topic_granularity,
        )
        self._capture_usage(generator)
        suffix = f" · {self.last_usage_summary}" if self.last_usage_summary else ""
        if self.last_result_warning:
            progress(100, f"{self.last_result_warning}{suffix}")
        else:
            progress(100, f"주제 다시 묶기가 완료되었습니다{suffix}.")
        return timeline.to_document()

    def analyze_vod(
        self,
        vod: Vod,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
        reusable_live_vods: Iterable[Vod] = (),
    ) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)

        generator = self._new_generator()
        self._preflight(generator, progress, cancelled)

        cache_path = analysis_data_dir(vod.vod_id) / "transcript.json"
        partial_path = analysis_data_dir(vod.vod_id) / "transcript.partial.json"
        transcript = load_vod_transcript_cache(
            cache_path,
            vod.vod_id,
            vod.url,
            self.config.whisper_model,
        )
        if transcript is None:
            partial = load_vod_transcript_cache(
                partial_path,
                vod.vod_id,
                vod.url,
                self.config.whisper_model,
            )
            from .vod_stream import fetch_vod_audio_source

            source = fetch_vod_audio_source(vod, progress, cancelled)
            live_reuse = build_live_replay_transcript_reuse(
                vod,
                reusable_live_vods,
                replay_duration=source.total_duration_seconds,
                replay_partial=partial,
            )
            transcriber = self._transcriber_factory(
                self.config.whisper_model,
                self.config.whisper_device,
            )
            prompt = build_whisper_prompt(vod, live=False)
            if live_reuse is not None:
                progress(
                    8,
                    f"같은 방송의 라이브 자막 {len(live_reuse.transcript.segments):,}개를 "
                    f"재사용합니다 · 중복 제외 "
                    f"{format_timestamp(covered_duration(live_reuse.covered_ranges))}",
                )

            def transcription_progress(percent: int, message: str) -> None:
                normalized = max(0, min(68, percent))
                progress(8 + int((normalized / 68) * 70), message)

            transcript = transcriber.transcribe_stream(
                source,
                initial_prompt=prompt,
                progress=transcription_progress,
                cancelled=cancelled,
                preview=preview,
                resume=partial,
                reusable=(
                    live_reuse.transcript
                    if live_reuse is not None
                    else None
                ),
                reusable_ranges=(
                    live_reuse.covered_ranges
                    if live_reuse is not None
                    else ()
                ),
                checkpoint=lambda snapshot: save_vod_transcript_cache(
                    partial_path,
                    vod.vod_id,
                    vod.url,
                    snapshot,
                ),
            )
            save_vod_transcript_cache(
                cache_path,
                vod.vod_id,
                vod.url,
                transcript,
            )
            partial_path.unlink(missing_ok=True)
        else:
            progress(
                78,
                f"저장된 자막 {len(transcript.segments):,}개 구간을 재사용합니다.",
            )
            if preview is not None:
                preview("transcript", transcript_preview_document(transcript.segments))

        if cancelled():
            from .transcription import AnalysisCancelled

            raise AnalysisCancelled("분석을 취소했습니다.")

        def generation_progress(percent: int, message: str) -> None:
            normalized = max(0, min(29, percent - 70))
            progress(80 + int((normalized / 29) * 19), message)

        progress(80, f"{self.provider_name} 타임라인 정리를 준비합니다…")
        timeline = self._generate_with_checkpoint(
            generator,
            vod,
            transcript,
            generation_progress,
            cancelled,
            preview,
            granularity=self.config.topic_granularity,
        )
        self._capture_usage(generator)
        suffix = f" · {self.last_usage_summary}" if self.last_usage_summary else ""
        if self.last_result_warning:
            progress(100, f"{self.last_result_warning}{suffix}")
        else:
            progress(100, f"AI 타임라인 생성이 완료되었습니다{suffix}.")
        return timeline.to_document()

    def transcribe_vod(
        self,
        vod: Vod,
        progress: ProgressCallback,
        cancelled: CancelCallback,
    ) -> None:
        """Run only faster-whisper and cache the transcript (no AI timeline).

        Used by background auto-processing so the slow STT is ready before the
        user decides to generate a timeline. Reuses any cached transcript and
        resumes from a partial capture if one exists.
        """
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            raise RuntimeError("faster-whisper가 설치되지 않았습니다.")

        cache_path = analysis_data_dir(vod.vod_id) / "transcript.json"
        partial_path = analysis_data_dir(vod.vod_id) / "transcript.partial.json"
        transcript = load_vod_transcript_cache(
            cache_path,
            vod.vod_id,
            vod.url,
            self.config.whisper_model,
        )
        if transcript is not None:
            progress(100, f"이미 저장된 자막 {len(transcript.segments):,}개가 있습니다.")
            return

        partial = load_vod_transcript_cache(
            partial_path,
            vod.vod_id,
            vod.url,
            self.config.whisper_model,
        )
        from .vod_stream import fetch_vod_audio_source

        source = fetch_vod_audio_source(vod, progress, cancelled)
        transcriber = self._transcriber_factory(
            self.config.whisper_model,
            self.config.whisper_device,
        )
        prompt = build_whisper_prompt(vod, live=False)

        def transcription_progress(percent: int, message: str) -> None:
            progress(max(0, min(99, percent)), message)

        transcript = transcriber.transcribe_stream(
            source,
            initial_prompt=prompt,
            progress=transcription_progress,
            cancelled=cancelled,
            preview=None,
            resume=partial,
            checkpoint=lambda snapshot: save_vod_transcript_cache(
                partial_path,
                vod.vod_id,
                vod.url,
                snapshot,
            ),
        )
        save_vod_transcript_cache(
            cache_path,
            vod.vod_id,
            vod.url,
            transcript,
        )
        partial_path.unlink(missing_ok=True)
        progress(100, f"자막 {len(transcript.segments):,}개 구간을 저장했습니다.")

    def analyze_live(
        self,
        vod: Vod,
        source: object,
        progress: ProgressCallback,
        stop_requested: CancelCallback,
        preview: PreviewCallback | None = None,
        finalize_requested: CancelCallback | None = None,
        resume_document: str = "",
    ) -> str:
        if not self.available:
            raise RuntimeError(self.unavailable_reason)
        from .live_stream import LiveAudioSource

        if not isinstance(source, LiveAudioSource):
            raise RuntimeError("라이브 오디오 소스 형식이 올바르지 않습니다.")
        transcriber = self._transcriber_factory(
            self.config.whisper_model,
            self.config.whisper_device,
        )
        generator = self._new_generator()
        self._preflight(generator, progress, stop_requested)
        prompt = build_whisper_prompt(vod, live=True)
        live_mode = live_ai_mode(self.config.live_ai_mode)
        should_finalize = finalize_requested or (lambda: True)

        def fast_stop_requested() -> bool:
            return stop_requested() and not should_finalize()

        progress(
            0,
            f"라이브 Gemini 모드: {live_mode.label} · "
            f"예상 시간당 약 {live_mode.estimated_calls_per_hour}회 + "
            "종료 시 타임라인 정리·전체 제목 각 1회",
        )
        # Live timestamps remain compatible if the user changes Whisper models
        # between launches, so never discard an earlier live capture here.
        prior_transcript = load_cached_transcript(vod)
        prior_segments = (
            list(prior_transcript.segments) if prior_transcript is not None else []
        )
        candidates, titles = _restore_live_timeline(
            resume_document,
            prior_segments,
        )
        summary_floor = source.runtime_seconds
        last_summary_end = source.runtime_seconds
        next_summary_at = source.runtime_seconds + live_mode.first_summary_seconds
        if prior_segments and prior_transcript is not None:
            summary_floor = prior_segments[0].start
            prior_end = max(
                float(prior_transcript.duration_seconds),
                prior_segments[-1].end,
            )
            saved_summary_end = _load_live_summary_watermark(vod)
            if saved_summary_end is not None:
                last_summary_end = max(
                    summary_floor,
                    min(saved_summary_end, prior_end),
                )
            elif candidates:
                # Older releases did not persist a summary watermark. Live
                # requests normally run once per configured interval, so
                # replay only a bounded tail instead of an entire long topic.
                inferred_tail = max(
                    300.0,
                    float(live_mode.interval_seconds)
                    + LIVE_TOPIC_CONFIRMATION_SECONDS
                    + LIVE_SUMMARY_OVERLAP_SECONDS,
                )
                last_summary_end = max(summary_floor, prior_end - inferred_tail)
            else:
                last_summary_end = summary_floor
            if prior_end > last_summary_end + 10:
                # The first post-reconnect Whisper update also summarizes any
                # durable text that had not reached Gemini before shutdown.
                next_summary_at = source.runtime_seconds
        transcript_journal = _LiveTranscriptJournal(
            vod,
            source,
            resume=prior_transcript,
        )

        def emit_timeline() -> None:
            if preview is None or not candidates:
                return
            preview(
                "live_timeline",
                GeneratedTimeline(
                    content_title=build_overall_summary(
                        vod,
                        titles,
                        candidates,
                    ),
                    entries=deduplicate_entries(candidates),
                ).to_document(),
            )

        def summarize_snapshot(snapshot: Transcript, *, force: bool = False) -> None:
            nonlocal candidates, last_summary_end, next_summary_at
            if fast_stop_requested() or not snapshot.segments:
                return
            latest_end = snapshot.segments[-1].end
            if not force and latest_end < next_summary_at:
                return
            stable_end = (
                latest_end
                if force
                else latest_end - LIVE_TOPIC_CONFIRMATION_SECONDS
            )
            if stable_end <= last_summary_end and candidates:
                next_summary_at = latest_end + live_mode.interval_seconds
                return
            window_start = max(
                summary_floor,
                last_summary_end - LIVE_SUMMARY_OVERLAP_SECONDS,
            )
            window = [
                segment
                for segment in snapshot.segments
                if segment.end >= window_start and segment.start <= stable_end
            ]
            if not window:
                return
            progress(
                0,
                f"라이브 수신을 계속하며 {self.provider_name}이(가) "
                f"{format_timestamp(window[0].start)}~"
                f"{format_timestamp(window[-1].end)} 구간을 정리합니다…",
            )
            try:
                partial = generator.summarize_live_window(
                    vod,
                    window,
                    fast_stop_requested,
                    previous_entries=deduplicate_entries(candidates)[-8:],
                )
            except AnalysisCancelled:
                if fast_stop_requested():
                    return
                raise
            except Exception as error:
                next_summary_at = latest_end + min(
                    live_mode.first_summary_seconds,
                    live_mode.interval_seconds,
                )
                progress(
                    0,
                    f"실시간 자막은 계속 작성 중 · {self.provider_name} 임시 정리 재시도 예정: {error}",
                )
                return
            if partial.content_title:
                titles.append(partial.content_title)
            candidates = deduplicate_entries(candidates + partial.entries)
            last_summary_end = stable_end
            next_summary_at = latest_end + live_mode.interval_seconds
            emit_timeline()
            _save_live_summary_watermark(vod, last_summary_end)

        # Whisper stays in its own consumer thread. Gemini can therefore wait,
        # retry, or time out without stopping the live HLS decoder and losing
        # audio after the small three-chunk capture buffer fills.
        updates: queue.Queue[object] = queue.Queue()
        transcription_done = object()
        transcription_result: list[Transcript] = []
        transcription_failure: list[BaseException] = []
        internal_stop = threading.Event()
        segment_offset = len(prior_segments)
        live_segments: list[TranscriptSegment] = list(prior_segments)
        live_language = (
            prior_transcript.language if prior_transcript is not None else "ko"
        )
        live_duration = (
            float(prior_transcript.duration_seconds)
            if prior_transcript is not None
            else 0.0
        )

        def effective_stop_requested() -> bool:
            return internal_stop.is_set() or stop_requested()

        def on_update(update: LiveTranscriptUpdate | Transcript) -> None:
            if isinstance(update, LiveTranscriptUpdate):
                normalized: LiveTranscriptUpdate | Transcript = (
                    _offset_live_update(update, segment_offset)
                )
                transcript_journal.append_update(normalized)
            else:
                current = _offset_live_transcript(update, segment_offset)
                normalized = _merge_live_transcripts(prior_transcript, current)
                transcript_journal.append(normalized)
            updates.put(normalized)

        def run_transcription() -> None:
            try:
                result = transcriber.transcribe_live(
                    source,
                    initial_prompt=prompt,
                    progress=progress,
                    stop_requested=effective_stop_requested,
                    preview=preview,
                    update=on_update,
                )
                transcription_result.append(result)
            except BaseException as error:
                transcription_failure.append(error)
            finally:
                updates.put(transcription_done)

        transcription_thread = threading.Thread(
            target=run_transcription,
            name=f"soop-live-whisper-{source.broadcast_no}",
            daemon=True,
        )
        transcription_thread.start()

        reached_end = False

        def apply_update(update: object) -> None:
            nonlocal live_language, live_duration
            if isinstance(update, LiveTranscriptUpdate):
                live_segments.extend(update.segments)
                live_language = update.language or live_language
                live_duration = max(live_duration, update.duration_seconds)
            elif isinstance(update, Transcript):
                # Compatibility for third-party/fake transcribers that still
                # provide cumulative snapshots.
                live_segments[:] = update.segments
                live_language = update.language or live_language
                live_duration = max(live_duration, update.duration_seconds)

        try:
            while not reached_end:
                message = updates.get()
                if message is transcription_done:
                    reached_end = True
                else:
                    apply_update(message)

                # Coalesce updates accumulated while a Gemini request was in
                # flight, so only the newest due snapshot is summarized.
                while not reached_end:
                    try:
                        message = updates.get_nowait()
                    except queue.Empty:
                        break
                    if message is transcription_done:
                        reached_end = True
                    else:
                        apply_update(message)

                if (
                    not reached_end
                    and not fast_stop_requested()
                    and live_segments
                    and live_segments[-1].end >= next_summary_at
                ):
                    summarize_snapshot(
                        Transcript(
                            model=self.config.whisper_model,
                            language=live_language,
                            duration_seconds=live_duration,
                            segments=list(live_segments),
                        )
                    )
        finally:
            internal_stop.set()
            transcription_thread.join(timeout=12.0)

        if transcription_failure:
            raise transcription_failure[0]
        if not transcription_result:
            raise RuntimeError("라이브 음성 인식 작업이 결과 없이 종료되었습니다.")
        current_transcript = _offset_live_transcript(
            transcription_result[0],
            segment_offset,
        )
        transcript = _merge_live_transcripts(prior_transcript, current_transcript)
        if not transcript.segments:
            if not should_finalize():
                raise AnalysisCancelled(
                    "프로그램 종료를 위해 라이브 자동 재연결 상태만 저장했습니다."
                )
            raise RuntimeError("라이브 방송에서 인식 가능한 음성을 찾지 못했습니다.")
        if not should_finalize():
            raise AnalysisCancelled(
                "프로그램 종료를 위해 새 Gemini 최종 요청 없이 라이브 자막만 저장했습니다."
            )
        transcript_journal.finalize(transcript)

        latest_end = transcript.segments[-1].end
        if not candidates or latest_end > last_summary_end + 10:
            summarize_snapshot(transcript, force=True)
        if not candidates:
            progress(
                0,
                f"누적 자막 전체를 {self.provider_name}이(가) 최종 타임라인으로 정리합니다…",
            )
            fallback = self._generate_with_checkpoint(
                generator,
                vod,
                transcript,
                progress,
                fast_stop_requested,
                None,
                granularity=self.config.topic_granularity,
            )
            if preview is not None:
                preview("live_timeline", fallback.to_document())
            self._capture_usage(generator)
            _clear_live_summary_watermark(vod)
            return fallback.to_document()

        progress(0, "라이브 타임라인의 중복과 전체 제목을 최종 정리합니다…")
        final = generator.finalize_live_entries(
            vod,
            titles,
            candidates,
            transcript.segments,
            fast_stop_requested,
        )
        if preview is not None:
            preview("live_timeline", final.to_document())
        self._capture_usage(generator)
        suffix = f" · {self.last_usage_summary}" if self.last_usage_summary else ""
        progress(100, f"라이브 타임라인 생성이 완료되었습니다{suffix}.")
        _clear_live_summary_watermark(vod)
        return final.to_document()


LocalWhisperAIAnalyzer = LocalWhisperGeminiAnalyzer


def _offset_live_update(
    update: LiveTranscriptUpdate,
    segment_offset: int,
) -> LiveTranscriptUpdate:
    """Make segment identifiers unique across application restarts."""

    segments: list[TranscriptSegment] = []
    for fallback_index, segment in enumerate(update.segments):
        raw_id = segment.segment_id
        local_index = fallback_index
        if raw_id.startswith("s") and raw_id[1:].isdigit():
            local_index = int(raw_id[1:])
        segments.append(
            TranscriptSegment(
                segment_id=f"s{segment_offset + local_index:06d}",
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
        )
    return LiveTranscriptUpdate(
        model=update.model,
        language=update.language,
        duration_seconds=update.duration_seconds,
        segments=tuple(segments),
        words=tuple(update.words),
    )


def _offset_live_transcript(
    transcript: Transcript,
    segment_offset: int,
) -> Transcript:
    return Transcript(
        model=transcript.model,
        language=transcript.language,
        duration_seconds=transcript.duration_seconds,
        segments=[
            TranscriptSegment(
                segment_id=f"s{segment_offset + index:06d}",
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
            for index, segment in enumerate(transcript.segments)
        ],
        words=tuple(transcript.words),
        covered_ranges=tuple(transcript.covered_ranges),
    )


def _merge_live_transcripts(
    earlier: Transcript | None,
    later: Transcript | None,
) -> Transcript:
    """Merge reconnect captures by absolute broadcast time and remove exact overlap."""

    if earlier is None and later is None:
        return Transcript("", "ko", 0.0, [], ())
    if earlier is None:
        assert later is not None
        return _offset_live_transcript(later, 0)
    if later is None:
        return _offset_live_transcript(earlier, 0)

    ordered_segments = sorted(
        [*earlier.segments, *later.segments],
        key=lambda segment: (segment.start, segment.end, segment.text),
    )
    merged_segments: list[TranscriptSegment] = []
    seen_segments: set[tuple[int, int, str]] = set()
    for segment in ordered_segments:
        key = (
            round(segment.start * 1_000),
            round(segment.end * 1_000),
            " ".join(segment.text.split()).casefold(),
        )
        if key in seen_segments:
            continue
        seen_segments.add(key)
        merged_segments.append(
            TranscriptSegment(
                segment_id=f"s{len(merged_segments):06d}",
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
        )

    ordered_words = sorted(
        [*earlier.words, *later.words],
        key=lambda word: (word.start, word.end, word.text),
    )
    merged_words: list[TranscriptWord] = []
    seen_words: set[tuple[int, int, str]] = set()
    for word in ordered_words:
        key = (
            round(word.start * 1_000),
            round(word.end * 1_000),
            word.text,
        )
        if key in seen_words:
            continue
        seen_words.add(key)
        merged_words.append(word)

    return Transcript(
        model=later.model or earlier.model,
        language=later.language or earlier.language,
        duration_seconds=max(
            float(earlier.duration_seconds),
            float(later.duration_seconds),
        ),
        segments=merged_segments,
        words=tuple(merged_words),
        covered_ranges=merge_covered_ranges(
            (*earlier.covered_ranges, *later.covered_ranges)
        ),
    )


def _restore_live_timeline(
    document: str,
    transcript_segments: list[TranscriptSegment],
) -> tuple[list[TimelineEntry], list[str]]:
    """Restore the saved live draft so reconnect previews append instead of replace."""

    if not document.strip():
        return [], []
    parsed = parse_timeline_document(document)
    starts = [segment.start for segment in transcript_segments]
    restored: list[TimelineEntry] = []

    for entry in parsed.entries:
        seconds = parse_timestamp(entry.timestamp)
        if seconds is None:
            continue
        segment_id = f"resume-{len(restored):06d}"
        if transcript_segments:
            insertion = bisect_left(starts, float(seconds))
            nearby = {
                max(0, min(len(transcript_segments) - 1, insertion + delta))
                for delta in (-2, -1, 0, 1)
            }

            def distance(index: int) -> float:
                segment = transcript_segments[index]
                if segment.start <= seconds <= segment.end:
                    return 0.0
                return min(abs(segment.start - seconds), abs(segment.end - seconds))

            nearest = min(nearby, key=distance)
            segment_id = transcript_segments[nearest].segment_id
        section_break = bool(
            restored
            and entry.line_index > 0
            and not parsed.lines[entry.line_index - 1].strip()
        )
        restored.append(
            TimelineEntry(
                segment_id=segment_id,
                start=float(seconds),
                summary=entry.summary,
                topic_key=entry.summary,
                section_break_before=section_break,
            )
        )

    titles = [parsed.content_title] if parsed.content_title.strip() else []
    return deduplicate_entries(restored), titles


def _load_live_summary_watermark(vod: Vod) -> float | None:
    path = analysis_data_dir(vod.vod_id) / LIVE_ANALYSIS_STATE_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or str(payload.get("url", "")) != vod.url
        ):
            return None
        return max(0.0, float(payload["last_summary_end"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_live_summary_watermark(vod: Vod, seconds: float) -> None:
    path = analysis_data_dir(vod.vod_id) / LIVE_ANALYSIS_STATE_FILENAME
    payload = {
        "version": 1,
        "url": vod.url,
        "last_summary_end": max(0.0, float(seconds)),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _clear_live_summary_watermark(vod: Vod) -> None:
    path = analysis_data_dir(vod.vod_id) / LIVE_ANALYSIS_STATE_FILENAME
    try:
        path.unlink()
    except OSError:
        pass


def _save_live_transcript_snapshot(
    vod: Vod,
    source: object,
    transcript: Transcript,
) -> None:
    destination = analysis_data_dir(vod.vod_id) / LIVE_TRANSCRIPT_FILENAME
    payload = {
        "source": {
            "kind": "soop_live",
            "url": vod.url,
            "runtime_start_seconds": float(
                getattr(source, "runtime_seconds", 0.0) or 0.0
            ),
        },
        "transcript": transcript.to_dict(),
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def _live_source_payload(vod: Vod, source: object) -> dict[str, object]:
    return {
        "kind": "soop_live",
        "url": vod.url,
        "runtime_start_seconds": float(
            getattr(source, "runtime_seconds", 0.0) or 0.0
        ),
    }


def _same_live_transcript(left: Transcript, right: Transcript) -> bool:
    if (
        len(left.segments) != len(right.segments)
        or len(left.words) != len(right.words)
        or abs(left.duration_seconds - right.duration_seconds) > 0.01
    ):
        return False
    if not left.segments:
        return True
    left_first, left_last = left.segments[0], left.segments[-1]
    right_first, right_last = right.segments[0], right.segments[-1]
    return (
        left_first.start == right_first.start
        and left_first.text == right_first.text
        and left_last.end == right_last.end
        and left_last.text == right_last.text
    )


class _LiveTranscriptJournal:
    """Append only newly recognized live text, then compact once at completion."""

    def __init__(
        self,
        vod: Vod,
        source: object,
        *,
        resume: Transcript | None = None,
    ):
        self.vod = vod
        self.source = source
        root = analysis_data_dir(vod.vod_id)
        self.path = root / LIVE_TRANSCRIPT_JOURNAL_FILENAME
        self.source_payload = _live_source_payload(vod, source)
        self.segment_count = 0
        self.word_count = 0
        self.duration_seconds = 0.0
        recovered = _load_live_transcript_journal(self.path, vod, None)
        if recovered is not None:
            if resume is not None and not _same_live_transcript(recovered, resume):
                self._reset()
                self.append(resume)
            else:
                self.segment_count = len(recovered.segments)
                self.word_count = len(recovered.words)
                self.duration_seconds = recovered.duration_seconds
        else:
            self._reset()
            if resume is not None:
                self.append(resume)

    def _reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "header",
            "version": 1,
            "source": self.source_payload,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(header, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
        self.segment_count = 0
        self.word_count = 0
        self.duration_seconds = 0.0

    def append(self, transcript: Transcript) -> None:
        if (
            len(transcript.segments) < self.segment_count
            or len(transcript.words) < self.word_count
        ):
            self._reset()
        new_segments = transcript.segments[self.segment_count :]
        new_words = transcript.words[self.word_count :]
        duration = float(transcript.duration_seconds)
        if (
            not new_segments
            and not new_words
            and duration <= self.duration_seconds
        ):
            return
        self._append_record(
            transcript.model,
            transcript.language,
            duration,
            new_segments,
            new_words,
        )
        self.segment_count = len(transcript.segments)
        self.word_count = len(transcript.words)
        self.duration_seconds = max(self.duration_seconds, duration)

    def append_update(self, update: LiveTranscriptUpdate) -> None:
        """Persist one live delta without reconstructing the full transcript."""
        if (
            self.segment_count
            and update.segments
            and update.segments[0].segment_id == "s000000"
        ):
            # A new capture was started against a stale journal path.
            self._reset()
        duration = float(update.duration_seconds)
        if (
            not update.segments
            and not update.words
            and duration <= self.duration_seconds
        ):
            return
        self._append_record(
            update.model,
            update.language,
            duration,
            update.segments,
            update.words,
        )
        self.segment_count += len(update.segments)
        self.word_count += len(update.words)
        self.duration_seconds = max(self.duration_seconds, duration)

    def _append_record(
        self,
        model: str,
        language: str,
        duration: float,
        segments: Iterable[TranscriptSegment],
        words: Iterable[TranscriptWord],
    ) -> None:
        record = {
            "type": "append",
            "model": model,
            "language": language,
            "duration_seconds": duration,
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                }
                for segment in segments
            ],
            "words": [
                {"start": word.start, "end": word.end, "text": word.text}
                for word in words
            ],
        }
        with self.path.open("a", encoding="utf-8", newline="\n") as journal:
            journal.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            journal.flush()

    def finalize(self, transcript: Transcript) -> None:
        _save_live_transcript_snapshot(self.vod, self.source, transcript)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def build_whisper_prompt(vod: Vod, *, live: bool) -> str:
    media = "라이브 방송" if live else "다시보기"
    prompt = (
        f"한국어 인터넷 {media}입니다. 스트리머는 {vod.streamer_name}이고 "
        f"제목은 {vod.title}입니다. 인명과 고유명사를 문맥에 맞게 적으세요."
    )
    glossary = " ".join(vod.streamer_glossary.split())[:2_000]
    if glossary:
        prompt += f" 자주 쓰는 고유명사 표기는 다음과 같습니다: {glossary}"
    return prompt


def timeline_checkpoint_key(
    vod: Vod,
    transcript: Transcript,
    model_name: str,
    granularity: str,
) -> str:
    digest = hashlib.sha256()
    metadata = {
        "vod_id": vod.vod_id,
        "url": vod.url,
        "transcript_model": transcript.model,
        "gemini_model": model_name,
        "granularity": granularity,
        "duration": transcript.duration_seconds,
        "glossary": vod.streamer_glossary,
    }
    digest.update(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for segment in transcript.segments:
        digest.update(
            f"\n{segment.segment_id}|{segment.start:.3f}|{segment.end:.3f}|{segment.text}".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def save_timeline_generation_state(
    path: str | Path,
    state: TimelineGenerationState,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(destination)


def load_timeline_generation_state(
    path: str | Path,
    expected_key: str,
) -> TimelineGenerationState | None:
    checkpoint_path = Path(path)
    if not checkpoint_path.is_file():
        return None
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        state = TimelineGenerationState.from_dict(payload)
        if not state.checkpoint_key or state.checkpoint_key != expected_key:
            return None
        return state
    except (OSError, ValueError, TypeError, KeyError):
        return None


def has_pending_timeline_finalization(vod_id: str) -> bool:
    path = analysis_data_dir(vod_id) / TIMELINE_CHECKPOINT_FILENAME
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and payload.get("stage") == "final_pending"
    except (OSError, ValueError, TypeError):
        return False


def remove_timeline_generation_checkpoint(vod_id: str) -> bool:
    """Remove AI topic-generation state without deleting the Whisper transcript."""
    path = analysis_data_dir(vod_id) / TIMELINE_CHECKPOINT_FILENAME
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def load_live_transcript_cache(
    path: str | Path,
    vod: Vod,
    expected_model: str | None,
) -> Transcript | None:
    cache_path = Path(path)
    snapshot = _load_live_transcript_snapshot(cache_path, vod, expected_model)
    journal_path = cache_path.with_name(LIVE_TRANSCRIPT_JOURNAL_FILENAME)
    journal = _load_live_transcript_journal(journal_path, vod, expected_model)
    if snapshot is not None and journal is not None:
        return _merge_live_transcripts(snapshot, journal)
    return journal or snapshot


def _load_live_transcript_snapshot(
    cache_path: Path,
    vod: Vod,
    expected_model: str | None,
) -> Transcript | None:
    if cache_path.is_file():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            source = payload.get("source", {})
            if (
                isinstance(source, dict)
                and source.get("kind") == "soop_live"
                and str(source.get("url", "")) == vod.url
            ):
                transcript = Transcript.from_dict(payload["transcript"])
                if (
                    (not expected_model or transcript.model == expected_model)
                    and transcript.segments
                ):
                    return transcript
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return None


def _load_live_transcript_journal(
    path: str | Path,
    vod: Vod,
    expected_model: str | None,
) -> Transcript | None:
    journal_path = Path(path)
    if not journal_path.is_file():
        return None
    try:
        lines = journal_path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
    except (OSError, UnicodeError):
        return None
    if not lines:
        return None
    try:
        header = json.loads(lines[0])
    except (ValueError, TypeError):
        return None
    source = header.get("source", {}) if isinstance(header, dict) else {}
    if (
        not isinstance(source, dict)
        or source.get("kind") != "soop_live"
        or str(source.get("url", "")) != vod.url
    ):
        return None

    model = ""
    language = "ko"
    duration = 0.0
    segments: list[dict[str, object]] = []
    words: list[dict[str, object]] = []
    for line in lines[1:]:
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            # A process can stop in the middle of its final append. Earlier
            # complete records remain valid and are still useful for recovery.
            continue
        if not isinstance(record, dict) or record.get("type") != "append":
            continue
        model = str(record.get("model", model))
        language = str(record.get("language", language) or language)
        try:
            record_duration = float(record.get("duration_seconds", 0.0) or 0.0)
        except (ValueError, TypeError):
            record_duration = 0.0
        duration = max(duration, record_duration)
        raw_segments = record.get("segments", [])
        raw_words = record.get("words", [])
        if isinstance(raw_segments, list):
            segments.extend(item for item in raw_segments if isinstance(item, dict))
        if isinstance(raw_words, list):
            words.extend(item for item in raw_words if isinstance(item, dict))
    if not segments or (expected_model and model != expected_model):
        return None
    try:
        return Transcript.from_dict(
            {
                "model": model,
                "language": language,
                "duration_seconds": duration,
                "segments": segments,
                "words": words,
            }
        )
    except (ValueError, TypeError, KeyError):
        return None


def load_cached_transcript(
    vod: Vod,
    expected_model: str | None = None,
) -> Transcript | None:
    root = analysis_data_dir(vod.vod_id)
    if vod.source_kind == "live":
        return load_live_transcript_cache(
            root / LIVE_TRANSCRIPT_FILENAME,
            vod,
            expected_model,
        )
    cache_path = root / "transcript.json"
    if expected_model:
        return load_vod_transcript_cache(
            cache_path,
            vod.vod_id,
            vod.url,
            expected_model,
        )
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        expected_source = {
            "kind": "soop_vod",
            "vod_id": vod.vod_id,
            "url": vod.url,
        }
        if payload.get("source") != expected_source:
            return None
        transcript = Transcript.from_dict(payload["transcript"])
        return transcript if transcript.segments else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def build_live_replay_transcript_reuse(
    replay_vod: Vod,
    live_vods: Iterable[Vod],
    *,
    replay_duration: float,
    replay_partial: Transcript | None = None,
) -> LiveReplayTranscriptReuse | None:
    """Reuse live STT only when the completed replay's time axis is verified.

    An exact SOOP broadcast number is mandatory. Alignment is accepted when a
    live capture reaches the replay ending, or when an existing replay partial
    transcript contains matching speech at the same timestamps.
    """

    total = max(0.0, float(replay_duration))
    broadcast_no = replay_vod.live_broadcast_no.strip()
    if total <= 0 or not broadcast_no or replay_vod.source_kind == "live":
        return None

    candidates: list[
        tuple[Vod, Transcript, tuple[tuple[float, float], ...]]
    ] = []
    alignment_verified = False
    end_tolerance = min(300.0, max(45.0, total * 0.005))
    for live_vod in live_vods:
        if (
            live_vod.source_kind != "live"
            or live_vod.streamer_id != replay_vod.streamer_id
            or live_vod.live_broadcast_no.strip() != broadcast_no
        ):
            continue
        transcript = load_cached_transcript(live_vod)
        if transcript is None or not transcript.segments:
            continue
        coverage = _live_capture_coverage(
            live_vod,
            transcript,
            replay_duration=total,
        )
        if not coverage:
            continue
        capture_end = max(end for _start, end in coverage)
        if 0.0 <= total - capture_end <= 5.0:
            coverage = merge_covered_ranges(
                (*coverage, (capture_end, total)),
                total_duration=total,
            )
        candidates.append((live_vod, transcript, coverage))
        capture_end = max(end for _start, end in coverage)
        if abs(total - capture_end) <= end_tolerance:
            alignment_verified = True
        elif replay_partial is not None and _transcripts_share_time_axis(
            replay_partial,
            transcript,
            coverage,
        ):
            alignment_verified = True

    if not candidates or not alignment_verified:
        return None

    candidates.sort(key=lambda item: item[2][0][0])
    used_ranges: tuple[tuple[float, float], ...] = ()
    merged: Transcript | None = None
    used_sessions = 0
    for _live_vod, transcript, coverage in candidates:
        new_ranges = _subtract_covered_ranges(coverage, used_ranges)
        if not new_ranges:
            continue
        filtered = _filter_transcript_to_ranges(
            transcript,
            new_ranges,
            replay_duration=total,
        )
        if not filtered.segments:
            continue
        merged = _merge_live_transcripts(merged, filtered)
        used_ranges = merge_covered_ranges(
            (*used_ranges, *new_ranges),
            total_duration=total,
        )
        used_sessions += 1

    if merged is None or not merged.segments or not used_ranges:
        return None
    merged.covered_ranges = used_ranges
    return LiveReplayTranscriptReuse(
        transcript=merged,
        covered_ranges=used_ranges,
        session_count=used_sessions,
    )


def _live_capture_coverage(
    vod: Vod,
    transcript: Transcript,
    *,
    replay_duration: float,
) -> tuple[tuple[float, float], ...]:
    total = max(0.0, float(replay_duration))
    capture_end = min(
        total,
        max(
            float(transcript.duration_seconds),
            max((segment.end for segment in transcript.segments), default=0.0),
        ),
    )
    if capture_end <= 0:
        return ()

    starts: list[float] = []
    duration_text = vod.duration_text.strip()
    if duration_text.startswith("시작 "):
        parsed = parse_timestamp(duration_text[3:].strip())
        if parsed is not None:
            starts.append(float(parsed))

    root = analysis_data_dir(vod.vod_id)
    for path in (
        root / LIVE_TRANSCRIPT_FILENAME,
        root / LIVE_TRANSCRIPT_JOURNAL_FILENAME,
    ):
        try:
            if path.name == LIVE_TRANSCRIPT_FILENAME:
                payload = json.loads(path.read_text(encoding="utf-8"))
            else:
                first_line = path.read_text(
                    encoding="utf-8",
                    errors="ignore",
                ).splitlines()[0]
                payload = json.loads(first_line)
            source = payload.get("source", {}) if isinstance(payload, dict) else {}
            if isinstance(source, dict) and "runtime_start_seconds" in source:
                runtime_start = float(source["runtime_start_seconds"])
                if runtime_start >= 0:
                    starts.append(runtime_start)
        except (OSError, IndexError, ValueError, TypeError):
            continue

    if transcript.segments:
        starts.append(max(0.0, float(transcript.segments[0].start)))
    capture_start = min(starts) if starts else 0.0
    capture_start = min(capture_end, max(0.0, capture_start))
    coverage: tuple[tuple[float, float], ...] = (
        (capture_start, capture_end),
    )

    gaps: list[tuple[float, float]] = []
    gap_path = root / LIVE_RECONNECT_LOG_FILENAME
    try:
        lines = gap_path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            record = json.loads(line)
            if (
                not isinstance(record, dict)
                or record.get("type") != "reconnect_gap"
                or str(record.get("broadcast_no", "")) != vod.live_broadcast_no
            ):
                continue
            gaps.append(
                (
                    float(record["missing_start_seconds"]),
                    float(record["missing_end_seconds"]),
                )
            )
        except (ValueError, TypeError, KeyError):
            continue
    return _subtract_covered_ranges(coverage, gaps)


def _subtract_covered_ranges(
    ranges: Iterable[tuple[float, float]],
    exclusions: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    remaining = list(merge_covered_ranges(ranges))
    for excluded_start, excluded_end in merge_covered_ranges(exclusions):
        updated: list[tuple[float, float]] = []
        for start, end in remaining:
            if excluded_end <= start or excluded_start >= end:
                updated.append((start, end))
                continue
            if excluded_start > start:
                updated.append((start, min(end, excluded_start)))
            if excluded_end < end:
                updated.append((max(start, excluded_end), end))
        remaining = updated
    return merge_covered_ranges(remaining)


def _filter_transcript_to_ranges(
    transcript: Transcript,
    ranges: Iterable[tuple[float, float]],
    *,
    replay_duration: float,
) -> Transcript:
    coverage = merge_covered_ranges(
        ranges,
        total_duration=replay_duration,
    )
    segments = [
        segment
        for segment in transcript.segments
        if any(
            start
            <= (segment.start + segment.end) / 2.0
            <= end
            for start, end in coverage
        )
    ]
    words = tuple(
        word
        for word in transcript.words
        if any(
            start <= (word.start + word.end) / 2.0 <= end
            for start, end in coverage
        )
    )
    return Transcript(
        model=transcript.model,
        language=transcript.language,
        duration_seconds=max((end for _start, end in coverage), default=0.0),
        segments=[
            TranscriptSegment(
                segment_id=f"s{index:06d}",
                start=segment.start,
                end=segment.end,
                text=segment.text,
            )
            for index, segment in enumerate(segments)
        ],
        words=words,
        covered_ranges=coverage,
    )


def _transcripts_share_time_axis(
    replay: Transcript,
    live: Transcript,
    live_ranges: Iterable[tuple[float, float]],
) -> bool:
    replay_end = max(
        float(replay.duration_seconds),
        max((segment.end for segment in replay.segments), default=0.0),
    )
    ranges = [
        (start, min(end, replay_end))
        for start, end in live_ranges
        if start < replay_end and min(end, replay_end) - start >= 60.0
    ]
    if not ranges:
        return False
    anchor_end = max(end for _start, end in ranges)
    anchor_start = max(
        min(start for start, _end in ranges),
        anchor_end - 8 * 60,
    )

    def sample(transcript: Transcript) -> str:
        text = " ".join(
            segment.text
            for segment in transcript.segments
            if anchor_start
            <= (segment.start + segment.end) / 2.0
            <= anchor_end
        )
        return "".join(character.casefold() for character in text if character.isalnum())[
            :12_000
        ]

    replay_text = sample(replay)
    live_text = sample(live)
    if len(replay_text) < 80 or len(live_text) < 80:
        return False
    return SequenceMatcher(
        None,
        replay_text,
        live_text,
        autojunk=True,
    ).ratio() >= 0.28


def live_capture_position(vod: Vod) -> float:
    """Return the last broadcast second durably processed by live STT."""

    if vod.source_kind != "live":
        return 0.0
    transcript = load_cached_transcript(vod)
    position = (
        float(transcript.duration_seconds) if transcript is not None else 0.0
    )
    root = analysis_data_dir(vod.vod_id)

    snapshot_path = root / LIVE_TRANSCRIPT_FILENAME
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        source = payload.get("source", {})
        if (
            isinstance(source, dict)
            and source.get("kind") == "soop_live"
            and str(source.get("url", "")) == vod.url
        ):
            raw_transcript = payload.get("transcript", {})
            if isinstance(raw_transcript, dict):
                position = max(
                    position,
                    float(raw_transcript.get("duration_seconds", 0.0) or 0.0),
                )
    except (OSError, ValueError, TypeError):
        pass

    journal_path = root / LIVE_TRANSCRIPT_JOURNAL_FILENAME
    try:
        lines = journal_path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines()
        header = json.loads(lines[0]) if lines else {}
        source = header.get("source", {}) if isinstance(header, dict) else {}
        if (
            isinstance(source, dict)
            and source.get("kind") == "soop_live"
            and str(source.get("url", "")) == vod.url
        ):
            for line in lines[1:]:
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and record.get("type") == "append":
                        position = max(
                            position,
                            float(record.get("duration_seconds", 0.0) or 0.0),
                        )
                except (ValueError, TypeError):
                    continue
    except (OSError, ValueError, TypeError):
        pass
    return max(0.0, position)


def record_live_reconnect_gap(
    vod: Vod,
    last_captured_seconds: float,
    resumed_runtime_seconds: float,
) -> float:
    """Append a structured record for audio that could not be observed offline."""

    start = max(0.0, float(last_captured_seconds))
    end = max(0.0, float(resumed_runtime_seconds))
    missing = max(0.0, end - start)
    if missing <= 1.0:
        return 0.0

    path = analysis_data_dir(vod.vod_id) / LIVE_RECONNECT_LOG_FILENAME
    record = {
        "type": "reconnect_gap",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vod_id": vod.vod_id,
        "broadcast_no": vod.live_broadcast_no,
        "missing_start_seconds": round(start, 3),
        "missing_end_seconds": round(end, 3),
        "missing_seconds": round(missing, 3),
        "missing_range": f"{format_timestamp(start)}~{format_timestamp(end)}",
    }

    # Starting the app repeatedly before a new chunk is captured must not
    # duplicate the same gap record.
    try:
        existing = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if existing:
            previous = json.loads(existing[-1])
            if (
                isinstance(previous, dict)
                and abs(
                    float(previous.get("missing_start_seconds", -1.0)) - start
                )
                <= 1.0
                and abs(float(previous.get("missing_end_seconds", -1.0)) - end)
                <= 1.0
            ):
                return missing
    except (OSError, ValueError, TypeError):
        pass

    with path.open("a", encoding="utf-8", newline="\n") as destination:
        destination.write(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        destination.flush()
    return missing
