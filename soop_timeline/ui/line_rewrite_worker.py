from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.gemini_line_rewrite import AITimelineLineRewriter
from ..services.transcription import AnalysisCancelled, Transcript


logger = logging.getLogger(__name__)


class TimelineLineRewriteWorker(QObject):
    usage_changed = Signal(str, str)
    succeeded = Signal(str, str, str, int)  # vod_id, original_line, new_line, start
    failed = Signal(str, str)
    cancelled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        rewriter: AITimelineLineRewriter,
        vod_id: str,
        mode: str,
        line: str,
        next_seconds: int,
        line_start: int,
        transcript: Transcript,
    ):
        super().__init__()
        self.rewriter = rewriter
        self.vod_id = vod_id
        self.mode = mode
        self.line = line
        self.next_seconds = next_seconds
        self.line_start = line_start
        self.transcript = transcript

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            result = self.rewriter.rewrite(
                self.mode,
                self.line,
                self.next_seconds,
                self.transcript,
                cancelled=thread.isInterruptionRequested,
            )
        except AnalysisCancelled:
            self.cancelled.emit(self.vod_id)
        except Exception as error:
            logger.exception("Timeline line rewrite failed for %s", self.vod_id)
            self.failed.emit(self.vod_id, str(error))
        else:
            summary = getattr(self.rewriter, "usage_summary", None)
            usage = str(summary()) if callable(summary) else ""
            if usage:
                self.usage_changed.emit(self.vod_id, usage)
            self.succeeded.emit(self.vod_id, self.line, result, self.line_start)
        finally:
            self.finished.emit()
