import tempfile
import unittest
from pathlib import Path

from soop_timeline.database import Database
from soop_timeline.models import VodState


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")

    def tearDown(self):
        self.database.close()
        self.temp_dir.cleanup()

    def test_discovery_deduplicates_vod_id(self):
        streamer = self.database.add_streamer("sample01", "샘플")
        item = {
            "vod_id": "12345",
            "title": "새 다시보기",
            "url": "https://vod.sooplive.com/player/12345",
            "duration": "2:30:00",
            "published": "1시간 전",
            "thumbnail": "https://example.test/thumb.jpg",
        }
        self.assertEqual(self.database.upsert_discovered_vods(streamer.id, [item]), 1)
        self.assertEqual(self.database.upsert_discovered_vods(streamer.id, [item]), 0)
        self.assertEqual(len(self.database.list_vods()), 1)

    def test_timeline_is_saved_and_state_is_preserved(self):
        streamer = self.database.add_streamer("sample02")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "77",
                    "title": "방송",
                    "url": "https://vod.sooplive.com/player/77",
                }
            ],
        )
        text = "오늘의 콘텐츠: 테스트\n\n00:01:00 시작"
        self.database.save_timeline("77", text)
        self.database.set_vod_state("77", VodState.READY.value)

        document = self.database.get_timeline("77")
        vod = self.database.get_vod("77")
        self.assertIsNotNone(document)
        self.assertEqual(document.text, text)
        self.assertEqual(vod.state, VodState.READY.value)

    def test_settings_round_trip(self):
        self.assertEqual(self.database.get_setting("missing", "default"), "default")
        self.database.set_setting("whisper_model", "large-v3")
        self.assertEqual(self.database.get_setting("whisper_model"), "large-v3")
        self.database.set_setting("whisper_model", "large-v3-turbo")
        self.assertEqual(self.database.get_setting("whisper_model"), "large-v3-turbo")

    def test_manual_vod_does_not_enable_automatic_streamer_check(self):
        vod = self.database.upsert_external_vod(
            vod_id="manual-1",
            channel_id="manual_source",
            streamer_name="수동 스트리머",
            title="수동 링크",
            url="https://play.sooplive.com/manual_source/123",
            source_kind="live",
        )

        self.assertEqual(vod.source_kind, "live")
        self.assertEqual(self.database.list_streamers(enabled_only=True), [])
        self.assertEqual(len(self.database.list_streamers()), 1)

    def test_timeline_revisions_and_analysis_queue_round_trip(self):
        streamer = self.database.add_streamer("queue-user", "대기열")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "9901",
                    "title": "대기 영상",
                    "url": "https://vod.sooplive.com/player/9901",
                }
            ],
        )
        self.database.create_timeline_revision("9901", "첫 버전", "테스트")
        self.database.enqueue_analysis("9901")

        revisions = self.database.list_timeline_revisions("9901")
        self.assertEqual(revisions[0].text, "첫 버전")
        self.assertEqual(self.database.recover_analysis_queue(), ["9901"])
        self.database.remove_analysis_queue("9901")
        self.assertEqual(self.database.list_analysis_queue(), [])

    def test_streamer_glossary_is_available_on_vod(self):
        streamer = self.database.add_streamer("glossary-user", "단어사전")
        self.database.update_streamer_glossary(
            streamer.id,
            "마이곰이\n월드 오브 워크래프트",
        )
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "8801",
                    "title": "고유명사 테스트",
                    "url": "https://vod.sooplive.com/player/8801",
                }
            ],
        )
        vod = self.database.get_vod("8801")
        self.assertIn("마이곰이", vod.streamer_glossary)
        self.assertEqual(
            self.database.list_streamers(enabled_only=True)[0].glossary,
            "마이곰이\n월드 오브 워크래프트",
        )

    def test_stale_live_session_is_marked_failed_for_recovery(self):
        vod = self.database.upsert_external_vod(
            vod_id="live-stale",
            channel_id="live-user",
            streamer_name="라이브",
            title="중단된 라이브",
            url="https://play.sooplive.com/live-user/1",
            source_kind="live",
            state=VodState.ANALYZING.value,
        )
        self.assertEqual(self.database.recover_stale_live_sessions(), [vod.vod_id])
        self.assertEqual(self.database.get_vod(vod.vod_id).state, VodState.FAILED.value)


if __name__ == "__main__":
    unittest.main()
