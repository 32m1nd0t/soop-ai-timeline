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


if __name__ == "__main__":
    unittest.main()
