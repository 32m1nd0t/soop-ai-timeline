from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
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
from .transcription import (
    AnalysisCancelled,
    CancelCallback,
    FasterWhisperTranscriber,
    LiveTranscriptUpdate,
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


@dataclass(slots=True, frozen=True)
class AnalyzerConfig:
    gemini_model: str = DEFAULT_GEMINI_MODEL
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_device: str = "auto"
    gemini_api_key: str = ""
    topic_granularity: str = DEFAULT_TOPIC_GRANULARITY
    live_ai_mode: str = "saving"


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
    ) -> str:
        raise NotImplementedError


class ReviewDraftAnalyzer(TimelineAnalyzer):
    """Creates the review document until the real audio analyzer is connected."""

    @property
    def available(self) -> bool:
        return False

    def initial_document(self, vod: Vod) -> str:
        return f"오늘의 콘텐츠: {vod.title}\n\n"

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
    ) -> str:
        del vod, progress, cancelled, preview
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
        return f"오늘의 콘텐츠: {vod.title}\n\n"

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
            transcriber = self._transcriber_factory(
                self.config.whisper_model,
                self.config.whisper_device,
            )
            prompt = build_whisper_prompt(vod, live=False)

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

    def analyze_live(
        self,
        vod: Vod,
        source: object,
        progress: ProgressCallback,
        stop_requested: CancelCallback,
        preview: PreviewCallback | None = None,
        finalize_requested: CancelCallback | None = None,
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
            f"예상 시간당 약 {live_mode.estimated_calls_per_hour}회 + 종료 시 최종 1회",
        )
        candidates: list[TimelineEntry] = []
        titles: list[str] = []
        last_summary_end = source.runtime_seconds
        next_summary_at = source.runtime_seconds + live_mode.first_summary_seconds
        transcript_journal = _LiveTranscriptJournal(vod, source)

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
                source.runtime_seconds,
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

        # Whisper stays in its own consumer thread. Gemini can therefore wait,
        # retry, or time out without stopping the live HLS decoder and losing
        # audio after the small three-chunk capture buffer fills.
        updates: queue.Queue[object] = queue.Queue()
        transcription_done = object()
        transcription_result: list[Transcript] = []
        transcription_failure: list[BaseException] = []
        internal_stop = threading.Event()

        def effective_stop_requested() -> bool:
            return internal_stop.is_set() or stop_requested()

        def on_update(update: LiveTranscriptUpdate | Transcript) -> None:
            if isinstance(update, LiveTranscriptUpdate):
                transcript_journal.append_update(update)
            else:
                transcript_journal.append(update)
            updates.put(update)

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

        live_segments: list[TranscriptSegment] = []
        live_language = "ko"
        live_duration = source.runtime_seconds
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
        transcript = transcription_result[0]
        if not transcript.segments:
            raise RuntimeError("라이브 방송에서 인식 가능한 음성을 찾지 못했습니다.")
        transcript_journal.finalize(transcript)
        if not should_finalize():
            raise AnalysisCancelled(
                "프로그램 종료를 위해 새 Gemini 최종 요청 없이 라이브 자막만 저장했습니다."
            )

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
        return final.to_document()


LocalWhisperAIAnalyzer = LocalWhisperGeminiAnalyzer


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


class _LiveTranscriptJournal:
    """Append only newly recognized live text, then compact once at completion."""

    def __init__(self, vod: Vod, source: object):
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
            self.segment_count = len(recovered.segments)
            self.word_count = len(recovered.words)
            self.duration_seconds = recovered.duration_seconds
        else:
            self._reset()

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
    journal_path = cache_path.with_name(LIVE_TRANSCRIPT_JOURNAL_FILENAME)
    return _load_live_transcript_journal(journal_path, vod, expected_model)


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
