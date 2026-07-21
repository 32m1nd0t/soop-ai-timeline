from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..models import Vod
from ..services.analyzer import LocalWhisperGeminiAnalyzer
from ..services.live_stream import LiveAudioSource
from ..services.transcription import AnalysisCancelled


class LiveAnalysisWorker(QObject):
    progress_changed = Signal(str, int, str)
    preview_changed = Signal(str, str, str)
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
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod.vod_id)
        except Exception as error:
            self.failed.emit(self.vod.vod_id, str(error))
        else:
            self.succeeded.emit(self.vod.vod_id, document)
        finally:
            self.finished.emit()
