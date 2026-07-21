from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.gemini_style import AITimelineStyler
from ..services.transcription import AnalysisCancelled


logger = logging.getLogger(__name__)


class TimelineStyleWorker(QObject):
    usage_changed = Signal(str, str)
    succeeded = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        styler: AITimelineStyler,
        vod_id: str,
        document: str,
    ):
        super().__init__()
        self.styler = styler
        self.vod_id = vod_id
        self.document = document

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            result = self.styler.restyle(
                self.document,
                cancelled=thread.isInterruptionRequested,
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod_id)
        except Exception as error:
            logger.exception("Timeline style correction failed for %s", self.vod_id)
            self.failed.emit(self.vod_id, str(error))
        else:
            summary = getattr(self.styler, "usage_summary", None)
            usage = str(summary()) if callable(summary) else ""
            if usage:
                self.usage_changed.emit(self.vod_id, usage)
            self.succeeded.emit(self.vod_id, result)
        finally:
            self.finished.emit()
