from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..models import Vod
from ..services.analyzer import LocalWhisperGeminiAnalyzer
from ..services.live_stream import LiveAudioSource
from ..services.transcription import AnalysisCancelled


logger = logging.getLogger(__name__)


class LiveAnalysisWorker(QObject):
    progress_changed = Signal(str, int, str)
    preview_changed = Signal(str, str, str)
    usage_changed = Signal(str, str)
    succeeded = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        analyzer: LocalWhisperGeminiAnalyzer,
        vod: Vod,
        source: LiveAudioSource,
    ):
        super().__init__()
        self.analyzer = analyzer
        self.vod = vod
        self.source = source
        self._skip_finalization = threading.Event()

    def request_stop(self, *, finalize: bool) -> None:
        """Choose whether interruption should run new Gemini final requests."""
        if not finalize:
            self._skip_finalization.set()

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()

        def progress(percent: int, message: str) -> None:
            self.progress_changed.emit(
                self.vod.vod_id,
                max(0, min(100, percent)),
                message,
            )

        def preview(stage: str, text: str) -> None:
            self.preview_changed.emit(self.vod.vod_id, stage, text)

        try:
            document = self.analyzer.analyze_live(
                self.vod,
                self.source,
                progress=progress,
                stop_requested=thread.isInterruptionRequested,
                preview=preview,
                finalize_requested=lambda: not self._skip_finalization.is_set(),
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod.vod_id)
        except Exception as error:
            logger.exception("Live analysis failed for %s", self.vod.vod_id)
            self.failed.emit(self.vod.vod_id, str(error))
        else:
            usage = str(getattr(self.analyzer, "last_usage_summary", "") or "")
            if usage:
                self.usage_changed.emit(self.vod.vod_id, usage)
            self.succeeded.emit(self.vod.vod_id, document)
        finally:
            self.finished.emit()
