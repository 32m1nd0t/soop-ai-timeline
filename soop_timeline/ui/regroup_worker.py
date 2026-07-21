from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..models import Vod
from ..services.analyzer import LocalWhisperGeminiAnalyzer
from ..services.transcription import AnalysisCancelled


logger = logging.getLogger(__name__)


class TimelineRegroupWorker(QObject):
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
        granularity: str,
    ):
        super().__init__()
        self.analyzer = analyzer
        self.vod = vod
        self.granularity = granularity

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
            document = self.analyzer.regroup_vod(
                self.vod,
                self.granularity,
                progress,
                thread.isInterruptionRequested,
                preview,
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod.vod_id)
        except Exception as error:
            logger.exception("Timeline regroup failed for %s", self.vod.vod_id)
            self.failed.emit(self.vod.vod_id, str(error))
        else:
            usage = str(getattr(self.analyzer, "last_usage_summary", "") or "")
            if usage:
                self.usage_changed.emit(self.vod.vod_id, usage)
            self.succeeded.emit(self.vod.vod_id, document)
        finally:
            self.finished.emit()
