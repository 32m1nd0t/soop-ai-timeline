import unittest

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Slot

from soop_timeline.models import Vod
from soop_timeline.ui.analysis_worker import AnalysisWorker


class _FakeAnalyzer:
    def analyze_vod(self, vod, progress, cancelled, preview):
        progress(50, "처리 중")
        preview("timeline", "00:00:01 중간 결과\n")
        return "오늘의 콘텐츠: 테스트\n\n00:00:01 완료\n"


class _Receiver(QObject):
    def __init__(self):
        super().__init__()
        self.result = None
        self.received_thread = None

    @Slot(str, str)
    def succeeded(self, vod_id: str, document: str) -> None:
        self.result = (vod_id, document)
        self.received_thread = QThread.currentThread()


class AnalysisWorkerTests(unittest.TestCase):
    def test_success_is_delivered_to_receiver_thread_with_vod_id(self):
        app = QCoreApplication.instance() or QCoreApplication([])
        vod = Vod(
            vod_id="123",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="테스트",
            url="https://vod.sooplive.com/player/123",
            duration_text="1:00:00",
            published_text="오늘",
            thumbnail_url="",
            state="new",
            discovered_at="",
            updated_at="",
        )
        thread = QThread()
        worker = AnalysisWorker(_FakeAnalyzer(), vod)
        receiver = _Receiver()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.succeeded.connect(receiver.succeeded)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(app.quit)
        thread.start()
        QTimer.singleShot(3_000, app.quit)
        app.exec()
        thread.wait(3_000)

        self.assertEqual(receiver.result, ("123", "오늘의 콘텐츠: 테스트\n\n00:00:01 완료\n"))
        self.assertIs(receiver.received_thread, app.thread())
        self.assertFalse(thread.isRunning())


if __name__ == "__main__":
    unittest.main()
