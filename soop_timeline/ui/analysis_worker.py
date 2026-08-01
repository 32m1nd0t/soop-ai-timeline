from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..models import Vod
from ..services.analyzer import TimelineAnalyzer
from ..services.transcription import AnalysisCancelled


logger = logging.getLogger(__name__)


class AnalysisWorker(QObject):
    progress_changed = Signal(int, str)
    preview_changed = Signal(str, str)
    usage_changed = Signal(str)
    succeeded = Signal(str, str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        analyzer: TimelineAnalyzer,
        vod: Vod,
        result_vod_id: str | None = None,
        reusable_live_vods: tuple[Vod, ...] = (),
    ):
        super().__init__()
        self.analyzer = analyzer
        self.vod = vod
        self.result_vod_id = result_vod_id or vod.vod_id
        self.reusable_live_vods = reusable_live_vods

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()

        def progress(percent: int, message: str) -> None:
            self.progress_changed.emit(max(0, min(100, percent)), message)

        def preview(stage: str, text: str) -> None:
            self.preview_changed.emit(stage, text)

        try:
            arguments = {
                "progress": progress,
                "cancelled": thread.isInterruptionRequested,
                "preview": preview,
            }
            if self.reusable_live_vods:
                arguments["reusable_live_vods"] = self.reusable_live_vods
            document = self.analyzer.analyze_vod(self.vod, **arguments)
        except AnalysisCancelled:
            self.cancelled.emit(self.result_vod_id)
        except Exception as error:
            logger.exception("VOD analysis failed for %s", self.vod.vod_id)
            self.failed.emit(self.result_vod_id, str(error))
        else:
            # A request may finish just after the user presses cancel.  Do not
            # publish that late response as a successful replacement document.
            if thread.isInterruptionRequested():
                self.cancelled.emit(self.result_vod_id)
            else:
                usage = str(getattr(self.analyzer, "last_usage_summary", "") or "")
                if usage:
                    self.usage_changed.emit(usage)
                self.succeeded.emit(self.result_vod_id, document)
        finally:
            self.finished.emit()


class PreTranscribeWorker(QObject):
    """Runs only faster-whisper for a VOD in the background (no Gemini)."""

    progress_changed = Signal(str, int, str)
    succeeded = Signal(str)
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        analyzer: TimelineAnalyzer,
        vod: Vod,
        reusable_live_vods: tuple[Vod, ...] = (),
    ):
        super().__init__()
        self.analyzer = analyzer
        self.vod = vod
        self.reusable_live_vods = reusable_live_vods

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()

        def progress(percent: int, message: str) -> None:
            self.progress_changed.emit(
                self.vod.vod_id,
                max(0, min(100, percent)),
                message,
            )

        try:
            arguments = {
                "progress": progress,
                "cancelled": thread.isInterruptionRequested,
            }
            if self.reusable_live_vods:
                arguments["reusable_live_vods"] = self.reusable_live_vods
            self.analyzer.transcribe_vod(self.vod, **arguments)
        except AnalysisCancelled:
            self.cancelled.emit(self.vod.vod_id)
        except Exception as error:
            logger.exception("Pre-transcribe failed for %s", self.vod.vod_id)
            self.failed.emit(self.vod.vod_id, str(error))
        else:
            if thread.isInterruptionRequested():
                self.cancelled.emit(self.vod.vod_id)
            else:
                self.succeeded.emit(self.vod.vod_id)
        finally:
            self.finished.emit()
