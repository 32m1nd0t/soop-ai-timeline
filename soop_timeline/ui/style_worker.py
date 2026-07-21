from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.gemini_style import GeminiTimelineStyler
from ..services.transcription import AnalysisCancelled


class TimelineStyleWorker(QObject):
    succeeded = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        styler: GeminiTimelineStyler,
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
            self.failed.emit(self.vod_id, str(error))
        else:
            self.succeeded.emit(self.vod_id, result)
        finally:
            self.finished.emit()
