from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.manual_link import resolve_manual_link


class ManualLinkWorker(QObject):
    resolved = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            result = resolve_manual_link(
                self.url,
                cancelled=thread.isInterruptionRequested,
            )
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.resolved.emit(result)
        finally:
            self.finished.emit()
