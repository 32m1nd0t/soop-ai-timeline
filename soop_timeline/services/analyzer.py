from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Callable

from ..models import Vod
from ..paths import analysis_data_dir
from .credentials import get_gemini_api_key
from .gemini_timeline import (
    DEFAULT_TOPIC_GRANULARITY,
    GeneratedTimeline,
    GeminiTimelineGenerator,
    TimelineEntry,
    deduplicate_entries,
)
from .transcription import (
    CancelCallback,
    FasterWhisperTranscriber,
    Transcript,
    format_timestamp,
    PreviewCallback,
    ProgressCallback,
    load_transcript_cache,
    load_vod_transcript_cache,
    save_transcript_cache,
    save_vod_transcript_cache,
    transcript_preview_document,
)


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_WHISPER_MODEL = "large-v3-turbo"
LIVE_FIRST_SUMMARY_SECONDS = 60
LIVE_SUMMARY_INTERVAL_SECONDS = 3 * 60
LIVE_SUMMARY_OVERLAP_SECONDS = 30
LIVE_TOPIC_CONFIRMATION_SECONDS = 30


@dataclass(slots=True, frozen=True)
class AnalyzerConfig:
    gemini_model: str = DEFAULT_GEMINI_MODEL
    whisper_model: str = DEFAULT_WHISPER_MODEL
    whisper_device: str = "auto"
    gemini_api_key: str = ""
    topic_granularity: str = DEFAULT_TOPIC_GRANULARITY


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
        generator_factory: Callable[[str, str], GeminiTimelineGenerator] | None = None,
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

    @classmethod
    def from_database(cls, database: object) -> "LocalWhisperGeminiAnalyzer":
        return cls(
            AnalyzerConfig(
                gemini_model=database.get_setting("gemini_model", DEFAULT_GEMINI_MODEL),
                whisper_model=database.get_setting("whisper_model", DEFAULT_WHISPER_MODEL),
                whisper_device=database.get_setting("whisper_device", "auto"),
                gemini_api_key=get_gemini_api_key(),
                topic_granularity=database.get_setting(
                    "topic_granularity",
                    DEFAULT_TOPIC_GRANULARITY,
                ),
            )
        )

    @property
    def available(self) -> bool:
        if not self.config.gemini_api_key:
            return False
        try:
            import faster_whisper  # noqa: F401
            from google import genai  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def unavailable_reason(self) -> str:
        if not self.config.gemini_api_key:
            return "설정에서 Gemini API 키를 입력하세요."
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return "faster-whisper가 설치되지 않았습니다."
        try:
            from google import genai  # noqa: F401
        except ImportError:
            return "google-genai가 설치되지 않았습니다."
        return ""

    def initial_document(self, vod: Vod) -> str:
        return f"오늘의 콘텐츠: {vod.title}\n\n"

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
            prompt = (
                f"한국어 인터넷 방송입니다. 스트리머는 {vod.streamer_name}이고 "
                f"영상 제목은 {vod.title}입니다."
            )
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

        generator = self._generator_factory(
            self.config.gemini_api_key,
            self.config.gemini_model,
        )
        timeline = generator.generate(
            vod,
            transcript,
            progress,
            cancelled,
            preview=preview,
        )
        progress(100, "AI 타임라인 생성이 완료되었습니다.")
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

        cache_path = analysis_data_dir(vod.vod_id) / "transcript.json"
        transcript = load_vod_transcript_cache(
            cache_path,
            vod.vod_id,
            vod.url,
            self.config.whisper_model,
        )
        if transcript is None:
            from .vod_stream import fetch_vod_audio_source

            source = fetch_vod_audio_source(vod, progress, cancelled)
            transcriber = self._transcriber_factory(
                self.config.whisper_model,
                self.config.whisper_device,
            )
            prompt = (
                f"한국어 인터넷 방송입니다. 스트리머는 {vod.streamer_name}이고 "
                f"영상 제목은 {vod.title}입니다. 인명과 고유명사를 문맥에 맞게 적으세요."
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
            )
            save_vod_transcript_cache(
                cache_path,
                vod.vod_id,
                vod.url,
                transcript,
            )
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

        generator = self._generator_factory(
            self.config.gemini_api_key,
            self.config.gemini_model,
        )

        def generation_progress(percent: int, message: str) -> None:
            normalized = max(0, min(29, percent - 70))
            progress(80 + int((normalized / 29) * 19), message)

        progress(80, "Gemini 타임라인 정리를 준비합니다…")
        timeline = generator.generate(
            vod,
            transcript,
            generation_progress,
            cancelled,
            preview=preview,
        )
        progress(100, "AI 타임라인 생성이 완료되었습니다.")
        return timeline.to_document()

    def analyze_live(
        self,
        vod: Vod,
        source: object,
        progress: ProgressCallback,
        stop_requested: CancelCallback,
        preview: PreviewCallback | None = None,
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
        generator = self._generator_factory(
            self.config.gemini_api_key,
            self.config.gemini_model,
        )
        prompt = (
            f"한국어 인터넷 라이브 방송입니다. 스트리머는 {vod.streamer_name}이고 "
            f"방송 제목은 {vod.title}입니다. 인명과 고유명사를 문맥에 맞게 적으세요."
        )
        candidates: list[TimelineEntry] = []
        titles: list[str] = []
        last_summary_end = source.runtime_seconds
        next_summary_at = source.runtime_seconds + LIVE_FIRST_SUMMARY_SECONDS
        last_snapshot: Transcript | None = None

        def emit_timeline() -> None:
            if preview is None or not candidates:
                return
            preview(
                "live_timeline",
                GeneratedTimeline(
                    content_title=titles[0] if titles else vod.title,
                    entries=deduplicate_entries(candidates),
                ).to_document(),
            )

        def summarize_snapshot(snapshot: Transcript, *, force: bool = False) -> None:
            nonlocal candidates, last_summary_end, next_summary_at
            if not snapshot.segments:
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
                next_summary_at = latest_end + LIVE_SUMMARY_INTERVAL_SECONDS
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
                "라이브 수신을 계속하며 Gemini가 "
                f"{format_timestamp(window[0].start)}~"
                f"{format_timestamp(window[-1].end)} 구간을 정리합니다…",
            )
            try:
                partial = generator.summarize_live_window(
                    vod,
                    window,
                    lambda: False,
                    previous_entries=deduplicate_entries(candidates)[-8:],
                )
            except Exception as error:
                next_summary_at = latest_end + LIVE_FIRST_SUMMARY_SECONDS
                progress(
                    0,
                    f"실시간 자막은 계속 작성 중 · Gemini 임시 정리 재시도 예정: {error}",
                )
                return
            if partial.content_title:
                titles.append(partial.content_title)
            candidates = deduplicate_entries(candidates + partial.entries)
            last_summary_end = stable_end
            next_summary_at = latest_end + LIVE_SUMMARY_INTERVAL_SECONDS
            emit_timeline()

        def on_update(snapshot: Transcript) -> None:
            nonlocal last_snapshot
            last_snapshot = snapshot
            _save_live_transcript_snapshot(vod, source, snapshot)
            summarize_snapshot(snapshot)

        transcript = transcriber.transcribe_live(
            source,
            initial_prompt=prompt,
            progress=progress,
            stop_requested=stop_requested,
            preview=preview,
            update=on_update,
        )
        last_snapshot = transcript
        if not transcript.segments:
            raise RuntimeError("라이브 방송에서 인식 가능한 음성을 찾지 못했습니다.")

        latest_end = transcript.segments[-1].end
        if not candidates or latest_end > last_summary_end + 10:
            summarize_snapshot(transcript, force=True)
        if not candidates:
            progress(0, "누적 자막 전체를 Gemini가 최종 타임라인으로 정리합니다…")
            fallback = generator.generate(
                vod,
                transcript,
                progress,
                lambda: False,
                preview=None,
            )
            if preview is not None:
                preview("live_timeline", fallback.to_document())
            return fallback.to_document()

        progress(0, "라이브 타임라인의 중복과 전체 제목을 최종 정리합니다…")
        final = generator.finalize_live_entries(
            vod,
            titles,
            candidates,
            last_snapshot.segments,
            lambda: False,
        )
        if preview is not None:
            preview("live_timeline", final.to_document())
        progress(100, "라이브 타임라인 생성이 완료되었습니다.")
        return final.to_document()


def _save_live_transcript_snapshot(
    vod: Vod,
    source: object,
    transcript: Transcript,
) -> None:
    destination = analysis_data_dir(vod.vod_id) / "live-transcript.json"
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
