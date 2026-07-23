import sqlite3
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

    def test_existing_database_is_migrated_for_memos_and_hidden_vods(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE streamers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                added_at TEXT NOT NULL,
                last_checked_at TEXT,
                last_error TEXT
            );
            CREATE TABLE vods (
                vod_id TEXT PRIMARY KEY,
                streamer_id INTEGER NOT NULL REFERENCES streamers(id),
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                duration_text TEXT NOT NULL DEFAULT '',
                published_text TEXT NOT NULL DEFAULT '',
                thumbnail_url TEXT NOT NULL DEFAULT '',
                source_kind TEXT NOT NULL DEFAULT 'vod',
                state TEXT NOT NULL DEFAULT 'new',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.close()

        migrated = Database(legacy_path)
        try:
            columns = {
                str(row["name"])
                for row in migrated.connection.execute(
                    "PRAGMA table_info(vods)"
                ).fetchall()
            }
        finally:
            migrated.close()

        self.assertIn("memo", columns)
        self.assertIn("hidden", columns)

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

    def test_vod_memo_persists_without_reopening_completed_work(self):
        streamer = self.database.add_streamer("memo-user", "메모")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "memo-1",
                    "title": "메모할 영상",
                    "url": "https://vod.sooplive.com/player/memo-1",
                }
            ],
        )
        self.database.set_vod_state("memo-1", VodState.READY.value)

        self.database.update_vod_memo("memo-1", "후반부 게임 구간 다시 확인")

        vod = self.database.get_vod("memo-1")
        self.assertEqual(vod.memo, "후반부 게임 구간 다시 확인")
        self.assertEqual(vod.state, VodState.READY.value)

    def test_hidden_vod_stays_hidden_after_discovery_and_can_be_restored(self):
        streamer = self.database.add_streamer("hidden-user", "숨김")
        item = {
            "vod_id": "hidden-1",
            "title": "숨길 영상",
            "url": "https://vod.sooplive.com/player/hidden-1",
        }
        self.database.upsert_discovered_vods(streamer.id, [item])
        self.database.update_vod_memo("hidden-1", "삭제하지 않을 메모")
        self.database.set_vod_hidden("hidden-1", True)

        self.assertEqual(self.database.list_vods(), [])
        hidden = self.database.list_vods(hidden=True)
        self.assertEqual([vod.vod_id for vod in hidden], ["hidden-1"])
        self.assertEqual(hidden[0].memo, "삭제하지 않을 메모")

        self.database.upsert_discovered_vods(streamer.id, [item])
        self.assertEqual(self.database.list_vods(), [])
        self.assertTrue(self.database.get_vod("hidden-1").hidden)

        self.database.set_vod_hidden("hidden-1", False)
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods()],
            ["hidden-1"],
        )

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

    def test_reset_vod_work_keeps_video_but_clears_generated_records(self):
        streamer = self.database.add_streamer("reset-user", "초기화")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "reset-1",
                    "title": "유지할 영상",
                    "url": "https://vod.sooplive.com/player/reset-1",
                }
            ],
        )
        self.database.save_timeline("reset-1", "오늘의 콘텐츠: 기존")
        self.database.create_timeline_revision("reset-1", "이전 버전", "테스트")
        self.database.enqueue_analysis("reset-1")

        self.database.reset_vod_work("reset-1")

        self.assertIsNotNone(self.database.get_vod("reset-1"))
        self.assertEqual(self.database.get_vod("reset-1").state, VodState.NEW.value)
        self.assertIsNone(self.database.get_timeline("reset-1"))
        self.assertEqual(self.database.list_timeline_revisions("reset-1"), [])
        self.assertEqual(self.database.list_analysis_queue(), [])
        self.assertIn("reset-1", [vod.vod_id for vod in self.database.list_vods()])

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

    def test_list_vods_filters_by_streamer_and_supports_sort_orders(self):
        first = self.database.add_streamer("first-user", "첫 번째")
        second = self.database.add_streamer("second-user", "두 번째")
        self.database.upsert_discovered_vods(
            first.id,
            [
                {
                    "vod_id": "100",
                    "title": "예전 영상",
                    "url": "https://vod.sooplive.com/player/100",
                },
                {
                    "vod_id": "300",
                    "title": "최근 영상",
                    "url": "https://vod.sooplive.com/player/300",
                },
            ],
        )
        self.database.upsert_discovered_vods(
            second.id,
            [
                {
                    "vod_id": "200",
                    "title": "다른 스트리머",
                    "url": "https://vod.sooplive.com/player/200",
                }
            ],
        )

        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(streamer_id=first.id)],
            ["300", "100"],
        )
        self.assertEqual(
            [
                vod.vod_id
                for vod in self.database.list_vods(
                    streamer_id=first.id,
                    sort="oldest",
                )
            ],
            ["100", "300"],
        )

    def test_finished_replay_is_linked_to_matching_live_session(self):
        streamer = self.database.add_streamer("live-link-user", "라이브 연결")
        live = self.database.upsert_external_vod(
            vod_id="live-900-20260723000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 여름 특집 방송",
            url="https://play.sooplive.com/live-link-user/900",
            source_kind="live",
            live_broadcast_no="900",
        )
        item = {
            "vod_id": "777001",
            "title": "여름 특집 방송 다시보기",
            "url": "https://vod.sooplive.com/player/777001",
        }
        self.database.upsert_discovered_vods(streamer.id, [item])

        links = self.database.auto_link_live_sessions(
            streamer.id,
            ["777001"],
            new_vod_ids=["777001"],
        )

        self.assertEqual(links, [(live.vod_id, "777001")])
        self.assertEqual(
            self.database.get_vod(live.vod_id).linked_vod_id,
            "777001",
        )


if __name__ == "__main__":
    unittest.main()
