import unittest

from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Slot

from soop_timeline.models import Vod
from soop_timeline.ui.analysis_worker import AnalysisWorker, PreTranscribeWorker


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
    def test_pretranscribe_worker_uses_only_fw_transcription(self):
        vod = Vod(
            vod_id="fw-only",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="FW 선행 분석",
            url="https://vod.sooplive.com/player/fw-only",
            duration_text="1:00:00",
            published_text="오늘",
            thumbnail_url="",
            state="queued",
            discovered_at="",
            updated_at="",
        )
        transcribed: list[str] = []
        succeeded: list[str] = []
        progress_updates: list[tuple[str, int, str]] = []

        class Analyzer:
            @staticmethod
            def transcribe_vod(vod, progress, cancelled):
                self.assertFalse(cancelled())
                progress(100, "완료")
                transcribed.append(vod.vod_id)

            @staticmethod
            def analyze_vod(*args, **kwargs):
                raise AssertionError("FW 선행 분석에서 Gemini를 호출하면 안 됩니다.")

        worker = PreTranscribeWorker(Analyzer(), vod)
        worker.succeeded.connect(succeeded.append)
        worker.progress_changed.connect(
            lambda vod_id, percent, message: progress_updates.append(
                (vod_id, percent, message)
            )
        )

        worker.run()

        self.assertEqual(transcribed, ["fw-only"])
        self.assertEqual(succeeded, ["fw-only"])
        self.assertEqual(progress_updates, [("fw-only", 100, "완료")])

    def test_linked_replay_passes_live_captures_to_analyzer(self):
        vod = Vod(
            vod_id="456",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="다시보기",
            url="https://vod.sooplive.com/player/456",
            duration_text="1:00:00",
            published_text="오늘",
            thumbnail_url="",
            state="new",
            discovered_at="",
            updated_at="",
        )
        live = Vod(
            vod_id="live-456",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="[LIVE] 방송",
            url="https://play.sooplive.com/sample/987",
            duration_text="시작 00:10:00",
            published_text="오늘",
            thumbnail_url="",
            state="review",
            discovered_at="",
            updated_at="",
            source_kind="live",
            live_broadcast_no="987",
        )
        received: list[tuple[Vod, ...]] = []

        class Analyzer:
            @staticmethod
            def analyze_vod(
                vod,
                progress,
                cancelled,
                preview,
                reusable_live_vods=(),
            ):
                del vod, progress, cancelled, preview
                received.append(tuple(reusable_live_vods))
                return "완료"

        worker = AnalysisWorker(
            Analyzer(),
            vod,
            reusable_live_vods=(live,),
        )
        worker.run()

        self.assertEqual(received, [(live,)])

    def test_pretranscribe_worker_can_pass_live_captures_to_stt_only_api(self):
        vod = Vod(
            vod_id="456",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="다시보기",
            url="https://vod.sooplive.com/player/456",
            duration_text="1:00:00",
            published_text="오늘",
            thumbnail_url="",
            state="new",
            discovered_at="",
            updated_at="",
        )
        live = Vod(
            vod_id="live-456",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="[LIVE] 방송",
            url="https://play.sooplive.com/sample/987",
            duration_text="시작 00:10:00",
            published_text="오늘",
            thumbnail_url="",
            state="review",
            discovered_at="",
            updated_at="",
            source_kind="live",
            live_broadcast_no="987",
        )
        received: list[tuple[Vod, ...]] = []

        class Analyzer:
            @staticmethod
            def transcribe_vod(
                vod,
                progress,
                cancelled,
                reusable_live_vods=(),
            ):
                del vod, progress, cancelled
                received.append(tuple(reusable_live_vods))

        worker = PreTranscribeWorker(
            Analyzer(),
            vod,
            reusable_live_vods=(live,),
        )
        worker.run()

        self.assertEqual(received, [(live,)])

    def test_custom_result_vod_id_routes_result_to_target_document(self):
        vod = Vod(
            vod_id="456",
            streamer_id=1,
            channel_id="sample",
            streamer_name="샘플",
            title="다시보기 원본",
            url="https://vod.sooplive.com/player/456",
            duration_text="1:00:00",
            published_text="오늘",
            thumbnail_url="",
            state="new",
            discovered_at="",
            updated_at="",
        )
        received: list[tuple[str, str]] = []
        worker = AnalysisWorker(
            _FakeAnalyzer(),
            vod,
            result_vod_id="live-456",
        )
        worker.succeeded.connect(
            lambda vod_id, document: received.append((vod_id, document))
        )

        worker.run()

        self.assertEqual(received[0][0], "live-456")

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
