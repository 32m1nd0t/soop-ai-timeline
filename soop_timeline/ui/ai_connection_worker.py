from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.ai_provider import GEMINI_PROVIDER, create_ai_provider


logger = logging.getLogger(__name__)


class AIConnectionTestWorker(QObject):
    succeeded = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, api_key: str, model_name: str):
        super().__init__()
        self.api_key = api_key
        self.model_name = model_name

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            client = create_ai_provider(
                GEMINI_PROVIDER,
                self.api_key,
                self.model_name,
            )
            message = client.test_connection(
                thread.isInterruptionRequested,
                force=True,
            )
        except Exception as error:
            logger.exception("Gemini connection test failed")
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(message)
        finally:
            self.finished.emit()
