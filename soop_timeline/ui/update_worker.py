from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..services.update_installer import (
    UpdateDownloadCancelled,
    download_verified_installer,
)


class UpdateInstallerDownloadWorker(QObject):
    progress = Signal(int, int)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(
        self,
        url: str,
        destination: Path,
        expected_sha256: str,
        user_agent: str,
    ) -> None:
        super().__init__()
        self.url = url
        self.destination = destination
        self.expected_sha256 = expected_sha256
        self.user_agent = user_agent

    @Slot()
    def run(self) -> None:
        thread = QThread.currentThread()
        try:
            path = download_verified_installer(
                self.url,
                self.destination,
                self.expected_sha256,
                user_agent=self.user_agent,
                cancelled=thread.isInterruptionRequested,
                progress=self.progress.emit,
            )
        except UpdateDownloadCancelled:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(str(error))
        else:
            self.succeeded.emit(str(path))
        finally:
            self.finished.emit()
