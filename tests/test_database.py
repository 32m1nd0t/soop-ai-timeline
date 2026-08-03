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

    def test_unchanged_timeline_save_keeps_edit_timestamp(self):
        streamer = self.database.add_streamer("stable-timestamp-user")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "78",
                    "title": "방송",
                    "url": "https://vod.sooplive.com/player/78",
                }
            ],
        )
        self.database.save_timeline("78", "변경 없는 본문", VodState.REVIEW.value)
        self.database.connection.execute(
            "UPDATE timeline_documents SET updated_at = ? WHERE vod_id = ?",
            ("2026-07-30T00:00:00.000001+00:00", "78"),
        )
        self.database.connection.commit()

        self.database.save_timeline("78", "변경 없는 본문", VodState.REVIEW.value)

        self.assertEqual(
            self.database.get_timeline("78").updated_at,
            "2026-07-30T00:00:00.000001+00:00",
        )

    def test_review_feedback_draft_and_examples_round_trip(self):
        streamer = self.database.add_streamer("feedback-user", "피드백")
        self.database.upsert_discovered_vods(
            streamer.id,
            [
                {
                    "vod_id": "feedback-1",
                    "title": "피드백 방송",
                    "url": "https://vod.sooplive.com/player/feedback-1",
                }
            ],
        )
        self.database.save_review_feedback_draft(
            "feedback-1",
            "오늘의 콘텐츠: 초안\n\n00:10:00 긴 문장",
        )

        count = self.database.replace_review_feedback_examples(
            "feedback-1",
            streamer.id,
            [
                ("rewrite", "00:10:00 긴 문장", "00:10:00 짧은 문장"),
                ("delete", "00:20:00 단순 잡담", ""),
            ],
        )

        self.assertEqual(count, 2)
        self.assertIn("초안", self.database.get_review_feedback_draft("feedback-1"))
        self.assertEqual(self.database.review_feedback_draft_count(), 1)
        examples = self.database.list_review_feedback_examples(
            streamer_id=streamer.id
        )
        self.assertEqual({item.action for item in examples}, {"rewrite", "delete"})
        self.assertEqual(self.database.review_feedback_example_count(), 2)

        replaced = self.database.replace_review_feedback_examples(
            "feedback-1",
            streamer.id,
            [("title", "기존 제목", "수정 제목")],
        )

        self.assertEqual(replaced, 1)
        self.assertEqual(
            [item.action for item in self.database.list_review_feedback_examples()],
            ["title"],
        )
        self.assertEqual(self.database.clear_review_feedback(), 1)
        self.assertEqual(self.database.review_feedback_example_count(), 0)
        self.assertEqual(self.database.review_feedback_draft_count(), 0)
        self.assertEqual(self.database.get_review_feedback_draft("feedback-1"), "")

    def test_lists_all_live_captures_for_exact_broadcast(self):
        first = self.database.upsert_external_vod(
            vod_id="live-one",
            channel_id="sample-live",
            streamer_name="샘플",
            title="[LIVE] 첫 연결",
            url="https://play.sooplive.com/sample-live/987",
            source_kind="live",
            live_broadcast_no="987",
        )
        self.database.upsert_external_vod(
            vod_id="live-two",
            channel_id="sample-live",
            streamer_name="샘플",
            title="[LIVE] 재연결",
            url="https://play.sooplive.com/sample-live/987",
            source_kind="live",
            live_broadcast_no="987",
        )
        self.database.upsert_external_vod(
            vod_id="live-other",
            channel_id="sample-live",
            streamer_name="샘플",
            title="[LIVE] 다른 방송",
            url="https://play.sooplive.com/sample-live/654",
            source_kind="live",
            live_broadcast_no="654",
        )

        sessions = self.database.list_live_sessions_for_broadcast(
            first.streamer_id,
            "987",
        )

        self.assertEqual(
            {vod.vod_id for vod in sessions},
            {"live-one", "live-two"},
        )

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

    def test_stale_live_session_remains_analyzing_for_auto_reconnect(self):
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
        self.assertEqual(
            self.database.get_vod(vod.vod_id).state,
            VodState.ANALYZING.value,
        )

    def test_hidden_or_linked_live_session_is_not_recovered(self):
        linked = self.database.upsert_external_vod(
            vod_id="live-linked-stale",
            channel_id="live-stale-filter-user",
            streamer_name="라이브",
            title="연결된 라이브",
            url="https://play.sooplive.com/live-stale-filter-user/11",
            source_kind="live",
            state=VodState.ANALYZING.value,
        )
        replay = self.database.upsert_external_vod(
            vod_id="777099",
            channel_id="live-stale-filter-user",
            streamer_name="라이브",
            title="연결된 다시보기",
            url="https://vod.sooplive.com/player/777099",
            source_kind="manual_vod",
        )
        hidden = self.database.upsert_external_vod(
            vod_id="live-hidden-stale",
            channel_id="live-stale-filter-user",
            streamer_name="라이브",
            title="숨긴 라이브",
            url="https://play.sooplive.com/live-stale-filter-user/12",
            source_kind="live",
            state=VodState.ANALYZING.value,
        )
        self.database.link_live_session_to_replay(linked.vod_id, replay.vod_id)
        self.database.connection.execute(
            "UPDATE vods SET hidden = 1 WHERE vod_id = ?", (hidden.vod_id,)
        )
        self.database.connection.commit()

        self.assertEqual(self.database.recover_stale_live_sessions(), [])

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

    def test_list_vods_supports_clickable_header_sort_orders(self):
        beta = self.database.add_streamer("beta-user", "Beta")
        alpha = self.database.add_streamer("alpha-user", "Alpha")
        self.database.upsert_discovered_vods(
            beta.id,
            [
                {
                    "vod_id": "10",
                    "title": "Zulu",
                    "url": "https://vod.sooplive.com/player/10",
                    "duration": "02:00",
                },
                {
                    "vod_id": "20",
                    "title": "Alpha",
                    "url": "https://vod.sooplive.com/player/20",
                    "duration": "00:30",
                },
            ],
        )
        self.database.upsert_discovered_vods(
            alpha.id,
            [
                {
                    "vod_id": "15",
                    "title": "Middle",
                    "url": "https://vod.sooplive.com/player/15",
                    "duration": "01:00:00",
                }
            ],
        )
        self.database.set_vod_state("10", VodState.READY.value)
        self.database.set_vod_state("20", VodState.ANALYZING.value)

        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(sort="title_asc")],
            ["20", "15", "10"],
        )
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(sort="duration_asc")],
            ["20", "10", "15"],
        )
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(sort="vod_id_desc")],
            ["20", "15", "10"],
        )
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(sort="streamer_asc")],
            ["15", "20", "10"],
        )
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods(sort="state_asc")][0],
            "20",
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

    def test_linked_live_work_is_migrated_to_single_replay_item(self):
        streamer = self.database.add_streamer("merge-live-user", "라이브 통합")
        live = self.database.upsert_external_vod(
            vod_id="live-904-20260726000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 통합할 방송",
            url="https://play.sooplive.com/merge-live-user/904",
            source_kind="live",
            live_broadcast_no="904",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777004",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="통합할 방송 다시보기",
            url="https://vod.sooplive.com/player/777004",
            source_kind="manual_vod",
        )
        self.database.save_timeline(
            live.vod_id,
            "라이브 최신 타임라인",
            VodState.READY.value,
        )
        self.database.set_vod_state(live.vod_id, VodState.READY.value)
        self.database.update_vod_memo(live.vod_id, "라이브 메모")
        self.database.create_timeline_revision(
            live.vod_id,
            "라이브 이전 버전",
            "라이브 자동 저장",
        )
        self.database.save_timeline(
            replay.vod_id,
            "다시보기 기존 초안",
            VodState.REVIEW.value,
        )
        self.database.connection.execute(
            "UPDATE timeline_documents SET updated_at = ? WHERE vod_id = ?",
            ("2026-07-26T00:00:00.000001+00:00", replay.vod_id),
        )
        self.database.connection.execute(
            "UPDATE timeline_documents SET updated_at = ? WHERE vod_id = ?",
            ("2026-07-26T00:00:00.000002+00:00", live.vod_id),
        )
        self.database.connection.commit()
        self.database.update_vod_memo(replay.vod_id, "다시보기 메모")
        self.database.link_live_session_to_replay(live.vod_id, replay.vod_id)

        self.assertEqual(
            self.database.list_pending_live_replay_migrations(),
            [(live.vod_id, replay.vod_id)],
        )
        self.assertEqual(
            [vod.vod_id for vod in self.database.list_vods()],
            [replay.vod_id],
        )

        self.assertTrue(
            self.database.migrate_live_session_work(live.vod_id, replay.vod_id)
        )
        self.assertFalse(
            self.database.migrate_live_session_work(live.vod_id, replay.vod_id)
        )

        migrated_replay = self.database.get_vod(replay.vod_id)
        retained_live = self.database.get_vod(live.vod_id)
        self.assertIsNotNone(migrated_replay)
        self.assertIsNotNone(retained_live)
        self.assertTrue(retained_live.hidden)
        self.assertEqual(migrated_replay.live_broadcast_no, "904")
        self.assertEqual(migrated_replay.state, VodState.READY.value)
        self.assertEqual(
            migrated_replay.memo,
            "다시보기 메모\n\n[라이브 작업에서 이전]\n라이브 메모",
        )
        self.assertEqual(
            self.database.get_timeline(replay.vod_id).text,
            "라이브 최신 타임라인",
        )
        revisions = self.database.list_timeline_revisions(replay.vod_id)
        self.assertIn("라이브 최신 타임라인", [item.text for item in revisions])
        self.assertIn("라이브 이전 버전", [item.text for item in revisions])
        self.assertIn("다시보기 기존 초안", [item.text for item in revisions])
        self.assertEqual(self.database.list_pending_live_replay_migrations(), [])

    def test_completed_replay_is_not_overwritten_by_delayed_live_migration(self):
        streamer = self.database.add_streamer("delayed-merge-user", "지연 통합")
        live = self.database.upsert_external_vod(
            vod_id="live-905-20260727000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 지연 통합 방송",
            url="https://play.sooplive.com/delayed-merge-user/905",
            source_kind="live",
            live_broadcast_no="905",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777005",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="지연 통합 방송 다시보기",
            url="https://vod.sooplive.com/player/777005",
            source_kind="manual_vod",
        )
        self.database.save_timeline(live.vod_id, "라이브 초안", VodState.REVIEW.value)
        self.database.save_timeline(
            replay.vod_id, "완료된 다시보기 전체 분석", VodState.READY.value
        )
        self.database.set_vod_state(replay.vod_id, VodState.READY.value)
        self.database.link_live_session_to_replay(live.vod_id, replay.vod_id)

        self.assertTrue(
            self.database.migrate_live_session_work(live.vod_id, replay.vod_id)
        )

        self.assertEqual(
            self.database.get_timeline(replay.vod_id).text,
            "완료된 다시보기 전체 분석",
        )
        self.assertEqual(
            self.database.get_vod(replay.vod_id).state,
            VodState.READY.value,
        )
        self.assertIn(
            "라이브 초안",
            [
                revision.text
                for revision in self.database.list_timeline_revisions(replay.vod_id)
            ],
        )

    def test_completed_replay_document_status_is_not_overwritten(self):
        streamer = self.database.add_streamer("newer-merge-user", "완료 통합")
        live = self.database.upsert_external_vod(
            vod_id="live-906-20260728000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 최신 통합 방송",
            url="https://play.sooplive.com/newer-merge-user/906",
            source_kind="live",
            live_broadcast_no="906",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777006",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="최신 통합 방송 다시보기",
            url="https://vod.sooplive.com/player/777006",
            source_kind="manual_vod",
        )
        self.database.save_timeline(live.vod_id, "오래된 라이브 초안")
        self.database.save_timeline(
            replay.vod_id,
            "방금 완료된 다시보기 분석",
            VodState.READY.value,
        )
        self.database.link_live_session_to_replay(live.vod_id, replay.vod_id)

        self.database.migrate_live_session_work(live.vod_id, replay.vod_id)

        self.assertEqual(
            self.database.get_timeline(replay.vod_id).text,
            "방금 완료된 다시보기 분석",
        )
        self.assertEqual(
            self.database.get_vod(replay.vod_id).state,
            VodState.READY.value,
        )

    def test_newer_replay_review_is_not_overwritten_by_live_migration(self):
        streamer = self.database.add_streamer("newer-review-user", "최신 검수본")
        live = self.database.upsert_external_vod(
            vod_id="live-907-20260729000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 최신 검수 방송",
            url="https://play.sooplive.com/newer-review-user/907",
            source_kind="live",
            live_broadcast_no="907",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777008",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="최신 검수 방송 다시보기",
            url="https://vod.sooplive.com/player/777008",
            source_kind="manual_vod",
        )
        self.database.save_timeline(live.vod_id, "이전 라이브 초안")
        self.database.save_timeline(replay.vod_id, "분석 전 다시보기 초안")
        self.database.create_timeline_revision(
            replay.vod_id,
            "분석 전 다시보기 초안",
            "AI 분석 전",
        )
        self.database.save_timeline(replay.vod_id, "새 다시보기 분석본")
        self.database.link_live_session_to_replay(live.vod_id, replay.vod_id)

        self.database.migrate_live_session_work(live.vod_id, replay.vod_id)

        self.assertEqual(
            self.database.get_timeline(replay.vod_id).text,
            "새 다시보기 분석본",
        )
        self.assertIn(
            "이전 라이브 초안",
            [
                revision.text
                for revision in self.database.list_timeline_revisions(replay.vod_id)
            ],
        )

    def test_migration_repairs_legacy_duplicate_replay_links(self):
        replay = self.database.upsert_external_vod(
            vod_id="777007",
            channel_id="duplicate-link-user",
            streamer_name="중복 연결",
            title="다시보기",
            url="https://vod.sooplive.com/player/777007",
            source_kind="manual_vod",
        )
        first = self.database.upsert_external_vod(
            vod_id="live-duplicate-one",
            channel_id="duplicate-link-user",
            streamer_name="중복 연결",
            title="첫 라이브",
            url="https://play.sooplive.com/duplicate-link-user/31",
            source_kind="live",
        )
        second = self.database.upsert_external_vod(
            vod_id="live-duplicate-two",
            channel_id="duplicate-link-user",
            streamer_name="중복 연결",
            title="두 번째 라이브",
            url="https://play.sooplive.com/duplicate-link-user/32",
            source_kind="live",
        )
        self.database.connection.execute("DROP INDEX idx_vods_unique_linked_replay")
        self.database.connection.execute(
            "UPDATE vods SET linked_vod_id = ? WHERE vod_id IN (?, ?)",
            (replay.vod_id, first.vod_id, second.vod_id),
        )
        self.database.connection.commit()
        database_path = self.database.path
        self.database.close()
        self.database = Database(database_path)

        linked_rows = self.database.connection.execute(
            "SELECT vod_id FROM vods WHERE linked_vod_id = ?",
            (replay.vod_id,),
        ).fetchall()
        self.assertEqual(len(linked_rows), 1)
        unlinked_vod_id = (
            second.vod_id
            if str(linked_rows[0]["vod_id"]) == first.vod_id
            else first.vod_id
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "UPDATE vods SET linked_vod_id = ? WHERE vod_id = ?",
                (replay.vod_id, unlinked_vod_id),
            )
        self.database.connection.rollback()

    def test_broadcast_number_links_replay_even_when_title_changed(self):
        streamer = self.database.add_streamer("title-change-user", "제목 변경")
        first = self.database.upsert_external_vod(
            vod_id="live-901-20260723000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 시작할 때 제목",
            url="https://play.sooplive.com/title-change-user/901",
            source_kind="live",
            live_broadcast_no="901",
        )
        self.database.upsert_external_vod(
            vod_id="live-902-20260724000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 다른 방송",
            url="https://play.sooplive.com/title-change-user/902",
            source_kind="live",
            live_broadcast_no="902",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777002",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="중간에 완전히 바꾼 제목",
            url="https://vod.sooplive.com/player/777002",
            source_kind="manual_vod",
            live_broadcast_no="901",
        )

        links = self.database.auto_link_live_sessions(
            streamer.id,
            [replay.vod_id],
        )

        self.assertEqual(links, [(first.vod_id, replay.vod_id)])

    def test_user_can_explicitly_link_unmatched_replay_to_live_session(self):
        streamer = self.database.add_streamer("manual-link-user", "수동 연결")
        live = self.database.upsert_external_vod(
            vod_id="live-903-20260725000000000000",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="[LIVE] 원래 제목",
            url="https://play.sooplive.com/manual-link-user/903",
            source_kind="live",
            live_broadcast_no="903",
        )
        replay = self.database.upsert_external_vod(
            vod_id="777003",
            channel_id=streamer.channel_id,
            streamer_name=streamer.display_name,
            title="전혀 다른 다시보기 제목",
            url="https://vod.sooplive.com/player/777003",
            source_kind="manual_vod",
        )

        self.database.link_live_session_to_replay(
            live.vod_id,
            replay.vod_id,
        )

        self.assertEqual(
            self.database.get_vod(live.vod_id).linked_vod_id,
            replay.vod_id,
        )


if __name__ == "__main__":
    unittest.main()
