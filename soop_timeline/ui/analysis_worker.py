from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..models import Vod
from ..services.analyzer import TimelineAnalyzer
from ..services.transcription import AnalysisCancelled


class AnalysisWorker(QObject):
    progress_changed = Signal(int, str)
    preview_changed = Signal(str, str)
    succeeded = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(self, analyzer: TimelineAnalyzer, vod: Vod):
        super().__init__()
        self.analyzer = analyzer
        self.vod = vod

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()

        def progress(percent: int, message: str) -> None:
            self.progress_changed.emit(max(0, min(100, percent)), message)

        def preview(stage: str, text: str) -> None:
            self.preview_changed.emit(stage, text)

        try:
            document = self.analyzer.analyze_vod(
                self.vod,
                progress=progress,
                cancelled=thread.isInterruptionRequested,
                preview=preview,
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod.vod_id)
        except Exception as error:
            self.failed.emit(self.vod.vod_id, str(error))
        else:
            self.succeeded.emit(self.vod.vod_id, document)
        finally:
            self.finished.emit()
