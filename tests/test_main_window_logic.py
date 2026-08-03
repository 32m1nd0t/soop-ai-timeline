import unittest
from types import SimpleNamespace
from unittest.mock import patch

from soop_timeline.models import VodState
from soop_timeline.services.manual_link import ResolvedVodLink
from soop_timeline.ui.main_window import MainWindow


class _TimelineDatabase:
    def __init__(self, *, state: str, text: str):
        self.vod = SimpleNamespace(vod_id="vod-1", state=state)
        self.timeline = SimpleNamespace(text=text, status=state)
        self.saved: list[tuple[str, str, str]] = []
        self.state_changes: list[tuple[str, str]] = []

    def get_vod(self, vod_id: str):
        del vod_id
        return self.vod

    def get_timeline(self, vod_id: str):
        del vod_id
        return self.timeline

    def save_timeline(self, vod_id: str, text: str, state: str):
        self.saved.append((vod_id, text, state))

    def set_vod_state(self, vod_id: str, state: str):
        self.state_changes.append((vod_id, state))


class MainWindowStateLogicTests(unittest.TestCase):
    def test_review_complete_learns_from_saved_ai_draft(self):
        saved: list[tuple[str, str, str]] = []
        states: list[tuple[str, str]] = []
        window_messages: list[str] = []
        editor_messages: list[str] = []

        class Database:
            @staticmethod
            def save_timeline(vod_id: str, text: str, state: str):
                saved.append((vod_id, text, state))

            @staticmethod
            def set_vod_state(vod_id: str, state: str):
                states.append((vod_id, state))

            @staticmethod
            def get_setting(key: str, default: str):
                del key, default
                return "1"

            @staticmethod
            def get_vod(vod_id: str):
                return SimpleNamespace(vod_id=vod_id)

        editor = SimpleNamespace(
            text=lambda: "검수 완료본",
            status_label=SimpleNamespace(setText=editor_messages.append),
        )
        window = SimpleNamespace(
            database=Database(),
            _editor_tabs={"vod-1": editor},
            status_label=SimpleNamespace(setText=window_messages.append),
            load_vods=lambda: None,
        )
        learning_result = SimpleNamespace(
            summary=lambda: "검수 피드백 2개를 저장했습니다."
        )

        with (
            patch(
                "soop_timeline.ui.main_window.learn_review_feedback",
                return_value=learning_result,
            ) as learn,
            patch(
                "soop_timeline.ui.main_window.QTimer.singleShot",
                side_effect=lambda delay, callback: callback(),
            ),
        ):
            MainWindow._mark_review_complete(window, "vod-1")

        self.assertEqual(
            saved,
            [("vod-1", "검수 완료본", VodState.READY.value)],
        )
        self.assertEqual(states, [("vod-1", VodState.READY.value)])
        learn.assert_called_once_with(
            window.database,
            window.database.get_vod("vod-1"),
            "검수 완료본",
        )
        expected = "검수 완료 · 검수 피드백 2개를 저장했습니다."
        self.assertEqual(window_messages, [expected])
        self.assertEqual(editor_messages, [expected])

    def test_opening_existing_completed_timeline_does_not_change_state(self):
        existing = "완료된 내용"
        database = _TimelineDatabase(state=VodState.READY.value, text=existing)
        window = SimpleNamespace(
            database=database,
            analyzer=SimpleNamespace(initial_document=lambda vod: "새 문서"),
        )

        text = MainWindow._load_or_create_timeline_text(window, database.vod)

        self.assertEqual(text, existing)
        self.assertEqual(database.saved, [])
        self.assertEqual(database.state_changes, [])

    def test_creating_new_timeline_saves_with_review_state(self):
        class _NoTimelineDatabase(_TimelineDatabase):
            def get_timeline(self, vod_id: str):
                del vod_id
                return None

        database = _NoTimelineDatabase(state=VodState.NEW.value, text="")
        window = SimpleNamespace(
            database=database,
            analyzer=SimpleNamespace(initial_document=lambda vod: "새 문서"),
        )

        text = MainWindow._load_or_create_timeline_text(window, database.vod)

        self.assertEqual(text, "새 문서")
        self.assertEqual(
            database.saved,
            [("vod-1", "새 문서", VodState.REVIEW.value)],
        )
        self.assertEqual(
            database.state_changes,
            [("vod-1", VodState.REVIEW.value)],
        )

    def test_unchanged_ready_timeline_stays_ready_when_editor_closes(self):
        database = _TimelineDatabase(state=VodState.READY.value, text="same")
        window = SimpleNamespace(_live_jobs={}, database=database)

        MainWindow._save_timeline(window, "vod-1", "same")

        self.assertEqual(
            database.saved,
            [("vod-1", "same", VodState.READY.value)],
        )
        self.assertEqual(database.state_changes, [])

    def test_editing_reviewed_timeline_reopens_review(self):
        database = _TimelineDatabase(state=VodState.READY.value, text="before")
        window = SimpleNamespace(_live_jobs={}, database=database)

        MainWindow._save_timeline(window, "vod-1", "after")

        self.assertEqual(
            database.saved,
            [("vod-1", "after", VodState.REVIEW.value)],
        )
        self.assertEqual(
            database.state_changes,
            [("vod-1", VodState.REVIEW.value)],
        )

    def test_double_click_opens_timeline_tab(self):
        class Item:
            @staticmethod
            def data(role):
                del role
                return "vod-77"

        opened: list[str] = []
        window = SimpleNamespace(
            vod_table=SimpleNamespace(
                item=lambda row, column: Item()
                if (row, column) == (3, 0)
                else None
            ),
            open_timeline=opened.append,
        )

        MainWindow.open_vod_from_row(window, 3, 4)

        self.assertEqual(opened, ["vod-77"])

    def test_vod_header_click_toggles_sort_direction(self):
        indicators: list[tuple[int, object]] = []
        shown: list[bool] = []
        placeholders: list[str] = []
        loaded: list[bool] = []

        header = SimpleNamespace(
            setSortIndicatorShown=shown.append,
            setSortIndicator=lambda column, order: indicators.append(
                (column, order)
            ),
        )
        combo = SimpleNamespace(
            blockSignals=lambda blocked: None,
            setCurrentIndex=lambda index: None,
            setPlaceholderText=placeholders.append,
        )
        window = SimpleNamespace(
            _VOD_HEADER_SORT_KEYS=MainWindow._VOD_HEADER_SORT_KEYS,
            _vod_header_sort_column=None,
            _vod_header_sort_ascending=True,
            vod_table=SimpleNamespace(horizontalHeader=lambda: header),
            sort_combo=combo,
            load_vods=lambda: loaded.append(True),
        )

        MainWindow._on_vod_header_clicked(window, 3)
        MainWindow._on_vod_header_clicked(window, 3)

        self.assertEqual(window._vod_header_sort_column, 3)
        self.assertFalse(window._vod_header_sort_ascending)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(shown, [True, True])
        self.assertIn("영상 제목 오름차순", placeholders[0])
        self.assertIn("영상 제목 내림차순", placeholders[1])
        self.assertNotEqual(indicators[0][1], indicators[1][1])

    def test_recovered_queue_item_is_not_removed_before_start_attempt(self):
        calls: list[tuple[str, bool, list[str]]] = []

        class QueueDatabase:
            @staticmethod
            def get_vod(vod_id: str):
                return SimpleNamespace(vod_id=vod_id, source_kind="vod")

        window = SimpleNamespace(
            _analysis_jobs={},
            _live_jobs={},
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _live_reconnect_job=None,
            _stale_live_sessions=[],
            _pretranscribe_jobs={},
            _pretranscribe_queue=[],
            _analysis_queue=["vod-1"],
            _editor_tabs={},
            database=QueueDatabase(),
        )
        window.open_timeline = lambda vod_id: None

        def start_analysis(vod_id: str, *, _from_queue: bool = False):
            calls.append((vod_id, _from_queue, list(window._analysis_queue)))

        window.start_analysis = start_analysis

        MainWindow._resume_persisted_analysis(window)

        self.assertEqual(calls, [("vod-1", True, ["vod-1"])])

    def test_fw_ready_queue_head_advances_while_next_fw_is_running(self):
        calls: list[str] = []

        class QueueDatabase:
            @staticmethod
            def get_vod(vod_id: str):
                return SimpleNamespace(vod_id=vod_id, source_kind="vod")

        window = SimpleNamespace(
            _analysis_jobs={},
            _live_jobs={},
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _live_reconnect_job=None,
            _stale_live_sessions=[],
            _pretranscribe_jobs={"vod-3": (object(), object())},
            _pretranscribe_queue=["vod-3"],
            _analysis_queue=["vod-2", "vod-3"],
            _editor_tabs={},
            database=QueueDatabase(),
            open_timeline=lambda vod_id: None,
            start_analysis=lambda vod_id, _from_queue=False: calls.append(vod_id),
        )

        MainWindow._resume_persisted_analysis(window)

        self.assertEqual(calls, ["vod-2"])

    def test_second_analysis_queues_fw_without_starting_another_ai_job(self):
        queued: list[str] = []
        state_changes: list[tuple[str, str]] = []
        editor_messages: list[str] = []
        status_messages: list[str] = []
        pretranscribe_resumes: list[bool] = []

        requested = SimpleNamespace(
            vod_id="vod-2",
            source_kind="vod",
            title="두 번째 영상",
            state=VodState.REVIEW.value,
        )
        running = SimpleNamespace(
            vod_id="vod-1",
            source_kind="vod",
            title="첫 번째 영상",
            state=VodState.ANALYZING.value,
        )

        class Database:
            @staticmethod
            def get_vod(vod_id: str):
                return requested if vod_id == "vod-2" else running

            @staticmethod
            def enqueue_analysis(vod_id: str):
                queued.append(vod_id)

            @staticmethod
            def set_vod_state(vod_id: str, state: str):
                state_changes.append((vod_id, state))

        editor = SimpleNamespace(
            status_label=SimpleNamespace(setText=editor_messages.append),
            set_analysis_queued=lambda message: editor_messages.append(message),
        )
        window = SimpleNamespace(
            database=Database(),
            _editor_tabs={"vod-2": editor},
            _live_reconnect_job=None,
            _stale_live_sessions=[],
            _live_jobs={},
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _analysis_jobs={"vod-1": (object(), object())},
            _analysis_source_ids={"vod-1": "vod-1"},
            _analysis_queue=[],
            _pretranscribe_jobs={},
            _pretranscribe_queue=[],
            status_label=SimpleNamespace(setText=status_messages.append),
            load_vods=lambda: None,
            _resume_pretranscribe_if_idle=lambda: pretranscribe_resumes.append(True),
        )

        MainWindow.start_analysis(window, "vod-2")

        self.assertEqual(window._analysis_queue, ["vod-2"])
        self.assertEqual(window._pretranscribe_queue, ["vod-2"])
        self.assertEqual(queued, ["vod-2"])
        self.assertEqual(
            state_changes,
            [("vod-2", VodState.QUEUED.value)],
        )
        self.assertEqual(pretranscribe_resumes, [True])
        self.assertIn("FW 자막추출", editor_messages[-1])
        self.assertIn("Gemini", editor_messages[-1])
        self.assertIn("FW 자막추출", status_messages[-1])

    def test_queued_analysis_is_reflected_in_an_opened_editor(self):
        queued_messages: list[str] = []
        editor = SimpleNamespace(
            _analysis_running=False,
            _live_running=False,
            set_analysis_queued=lambda message: queued_messages.append(message),
        )
        window = SimpleNamespace(
            _editor_tabs={"vod-2": editor},
            _live_jobs={},
            _analysis_jobs={},
            _analysis_source_ids={},
            _analysis_queue=["vod-1", "vod-2"],
            _pretranscribe_jobs={},
            _active_analysis_target_id=lambda vod_id: None,
        )

        MainWindow._sync_editor_analysis_state(window, "vod-2")

        self.assertEqual(len(queued_messages), 1)
        self.assertIn("대기열 2번째", queued_messages[0])

    def test_active_analysis_can_be_resolved_from_its_source_vod(self):
        window = SimpleNamespace(
            _analysis_jobs={"live-1": (object(), object())},
            _analysis_source_ids={"live-1": "vod-2"},
        )

        target = MainWindow._active_analysis_target_id(window, "vod-2")

        self.assertEqual(target, "live-1")

    def test_auxiliary_ai_job_is_reflected_in_an_opened_editor(self):
        cancel_labels: list[str] = []
        editor = SimpleNamespace(
            _analysis_running=False,
            _live_running=False,
            _auxiliary_ai_running=False,
            set_auxiliary_ai_running=lambda running, label="": cancel_labels.append(
                label if running else "stopped"
            ),
        )
        window = SimpleNamespace(
            _editor_tabs={"live-1": editor},
            _live_jobs={},
            _analysis_jobs={},
            _analysis_source_ids={},
            _analysis_queue=[],
            _active_analysis_target_id=lambda vod_id: None,
            _active_auxiliary_ai_job=lambda vod_id: (
                "live-1",
                "저장 자막 재정리",
                object(),
            ),
        )

        MainWindow._sync_editor_analysis_state(window, "live-1")

        self.assertEqual(cancel_labels, ["저장 자막 재정리 취소"])

    def test_linked_replay_resolves_to_live_regroup_job(self):
        thread = object()
        database = SimpleNamespace(
            get_vod=lambda vod_id: SimpleNamespace(linked_vod_id="vod-2")
        )
        window = SimpleNamespace(
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={"live-1": (thread, object())},
            database=database,
        )

        job = MainWindow._active_auxiliary_ai_job(window, "vod-2")

        self.assertEqual(job, ("live-1", "저장 자막 재정리", thread))

    def test_queued_analysis_cancel_updates_editor_and_interrupts_fw(self):
        interrupted: list[bool] = []
        removed: list[str] = []
        state_changes: list[tuple[str, str]] = []
        editor_running: list[bool] = []
        editor_messages: list[str] = []

        thread = SimpleNamespace(
            requestInterruption=lambda: interrupted.append(True)
        )
        editor = SimpleNamespace(
            set_analysis_running=editor_running.append,
            status_label=SimpleNamespace(setText=editor_messages.append),
        )
        database = SimpleNamespace(
            remove_analysis_queue=removed.append,
            set_vod_state=lambda vod_id, state: state_changes.append(
                (vod_id, state)
            ),
        )
        window = SimpleNamespace(
            _analysis_jobs={},
            _analysis_queue=["vod-2"],
            _pretranscribe_queue=["vod-2"],
            _pretranscribe_jobs={"vod-2": (thread, object())},
            _live_jobs={},
            _editor_tabs={"vod-2": editor},
            _active_analysis_target_id=lambda vod_id: None,
            database=database,
            status_label=SimpleNamespace(setText=lambda message: None),
            load_vods=lambda: None,
        )

        MainWindow._cancel_or_dequeue(window, "vod-2")

        self.assertEqual(window._analysis_queue, [])
        self.assertEqual(window._pretranscribe_queue, [])
        self.assertEqual(interrupted, [True])
        self.assertEqual(removed, ["vod-2"])
        self.assertEqual(
            state_changes,
            [("vod-2", VodState.REVIEW.value)],
        )
        self.assertEqual(editor_running, [False])
        self.assertIn("취소", editor_messages[-1])

    def test_running_analysis_cancel_accepts_source_vod_id(self):
        interrupted: list[bool] = []
        progress_messages: list[tuple[int, str]] = []
        thread = SimpleNamespace(
            requestInterruption=lambda: interrupted.append(True)
        )
        editor = SimpleNamespace(
            analysis_progress=SimpleNamespace(value=lambda: 42),
            set_analysis_progress=lambda percent, message: progress_messages.append(
                (percent, message)
            ),
        )
        window = SimpleNamespace(
            _live_jobs={},
            _analysis_jobs={"live-1": (thread, object())},
            _editor_tabs={"live-1": editor},
            _active_analysis_target_id=lambda vod_id: "live-1",
        )

        MainWindow.cancel_analysis(window, "vod-2")

        self.assertEqual(interrupted, [True])
        self.assertEqual(progress_messages[0][0], 42)
        self.assertIn("취소를 요청", progress_messages[0][1])

    def test_auxiliary_ai_cancel_interrupts_job_and_updates_button(self):
        interrupted: list[bool] = []
        cancel_requests: list[str] = []
        thread = SimpleNamespace(
            requestInterruption=lambda: interrupted.append(True)
        )
        editor = SimpleNamespace(
            request_auxiliary_ai_cancel=cancel_requests.append,
        )
        window = SimpleNamespace(
            _live_jobs={},
            _analysis_jobs={},
            _analysis_queue=[],
            _editor_tabs={"live-1": editor},
            _active_analysis_target_id=lambda vod_id: None,
            _active_auxiliary_ai_job=lambda vod_id: (
                "live-1",
                "저장 자막 재정리",
                thread,
            ),
            status_label=SimpleNamespace(setText=lambda message: None),
        )

        MainWindow._cancel_or_dequeue(window, "live-1")

        self.assertEqual(interrupted, [True])
        self.assertEqual(cancel_requests, ["저장 자막 재정리"])

    def test_pretranscribe_can_start_while_an_analysis_is_running(self):
        started: list[str] = []
        window = SimpleNamespace(
            _pretranscribe_jobs={},
            _pretranscribe_queue=["vod-2"],
            _MAX_CONCURRENT_PRETRANSCRIBES=3,
            _close_after_analysis=False,
            _stale_live_sessions=[],
            _live_reconnect_job=None,
            _analysis_jobs={"vod-1": (object(), object())},
            _analysis_queue=["vod-2"],
            _live_jobs={},
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _start_pretranscribe=started.append,
        )

        MainWindow._resume_pretranscribe_if_idle(window)

        self.assertEqual(started, ["vod-2"])

    def test_pretranscribe_starts_at_most_three_fw_jobs(self):
        started: list[str] = []
        jobs: dict[str, tuple[object, object]] = {}

        def start_pretranscribe(vod_id: str) -> None:
            started.append(vod_id)
            jobs[vod_id] = (object(), object())

        window = SimpleNamespace(
            _pretranscribe_jobs=jobs,
            _pretranscribe_queue=["vod-1", "vod-2", "vod-3", "vod-4"],
            _MAX_CONCURRENT_PRETRANSCRIBES=3,
            _close_after_analysis=False,
            _stale_live_sessions=[],
            _live_reconnect_job=None,
            _live_jobs={},
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _start_pretranscribe=start_pretranscribe,
        )

        MainWindow._resume_pretranscribe_if_idle(window)

        self.assertEqual(started, ["vod-1", "vod-2", "vod-3"])
        self.assertEqual(set(jobs), {"vod-1", "vod-2", "vod-3"})

    def test_all_pending_analyses_get_fw_preparation_while_ai_runs(self):
        window = SimpleNamespace(
            _close_after_analysis=False,
            _stale_live_sessions=[],
            _analysis_jobs={"vod-1": (object(), object())},
            _analysis_queue=["vod-2", "vod-3"],
            _pretranscribe_queue=[],
            _pretranscribe_attempted_ids=set(),
            _live_jobs={},
            _live_reconnect_job=None,
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _resume_pretranscribe_if_idle=lambda: None,
        )

        with patch(
            "soop_timeline.ui.main_window.QTimer.singleShot"
        ) as single_shot:
            MainWindow._resume_analysis_queue_if_idle(window)

        self.assertEqual(window._pretranscribe_queue, ["vod-2", "vod-3"])
        single_shot.assert_called_once()

    def test_completed_pretranscribe_is_not_requeued_while_ai_runs(self):
        window = SimpleNamespace(
            _close_after_analysis=False,
            _stale_live_sessions=[],
            _analysis_jobs={"vod-1": (object(), object())},
            _analysis_queue=["vod-2"],
            _pretranscribe_queue=[],
            _pretranscribe_attempted_ids={"vod-2"},
            _live_jobs={},
            _live_reconnect_job=None,
            _style_jobs={},
            _line_rewrite_jobs={},
            _regroup_jobs={},
            _manual_link_job=None,
            _resume_pretranscribe_if_idle=lambda: None,
        )

        with patch(
            "soop_timeline.ui.main_window.QTimer.singleShot"
        ) as single_shot:
            MainWindow._resume_analysis_queue_if_idle(window)

        self.assertEqual(window._pretranscribe_queue, [])
        single_shot.assert_not_called()

    def test_live_shutdown_preserves_session_for_next_launch(self):
        saved: list[tuple[str, str, str]] = []
        states: list[tuple[str, str]] = []
        running: list[bool] = []
        messages: list[str] = []

        class Database:
            @staticmethod
            def get_timeline(vod_id: str):
                del vod_id
                return SimpleNamespace(text="저장본")

            @staticmethod
            def save_timeline(vod_id: str, text: str, state: str):
                saved.append((vod_id, text, state))

            @staticmethod
            def set_vod_state(vod_id: str, state: str):
                states.append((vod_id, state))

        editor = SimpleNamespace(
            set_live_running=running.append,
            status_label=SimpleNamespace(setText=messages.append),
            text=lambda: "현재 라이브 타임라인",
        )
        window = SimpleNamespace(
            _editor_tabs={"live-1": editor},
            database=Database(),
        )

        MainWindow._preserve_live_for_restart(
            window,
            "live-1",
            "application shutdown",
        )

        self.assertEqual(running, [False])
        self.assertEqual(
            saved,
            [
                (
                    "live-1",
                    "현재 라이브 타임라인",
                    VodState.ANALYZING.value,
                )
            ],
        )
        self.assertEqual(
            states,
            [("live-1", VodState.ANALYZING.value)],
        )
        self.assertIn("자동 재연결", messages[0])

    def test_live_reconnect_terminal_errors_are_not_retried(self):
        self.assertTrue(
            MainWindow._is_terminal_live_reconnect_error(
                "입력한 라이브 방송이 종료되었거나 다른 방송으로 전환되었습니다."
            )
        )
        self.assertFalse(
            MainWindow._is_terminal_live_reconnect_error(
                "SOOP 라이브 정보 요청에 실패했습니다."
            )
        )

    def test_manual_live_reconnect_requeues_legacy_review_session(self):
        saved: list[tuple[str, str, str]] = []
        states: list[tuple[str, str]] = []
        pending: list[bool] = []
        retries: list[int] = []
        messages: list[str] = []

        class Database:
            @staticmethod
            def get_vod(vod_id: str):
                return SimpleNamespace(
                    vod_id=vod_id,
                    source_kind="live",
                    state=VodState.REVIEW.value,
                    streamer_name="테스트 스트리머",
                )

            @staticmethod
            def save_timeline(vod_id: str, text: str, state: str):
                saved.append((vod_id, text, state))

            @staticmethod
            def set_vod_state(vod_id: str, state: str):
                states.append((vod_id, state))

        editor = SimpleNamespace(
            text=lambda: "기존 라이브 타임라인",
            set_live_reconnect_pending=pending.append,
            status_label=SimpleNamespace(setText=messages.append),
        )
        window = SimpleNamespace(
            database=Database(),
            _editor_tabs={"live-1": editor},
            _live_jobs={},
            _live_reconnect_target_id="",
            _stale_live_sessions=[],
            _live_reconnect_attempts={"live-1": 3},
            status_label=SimpleNamespace(setText=messages.append),
            load_vods=lambda: None,
            _schedule_live_reconnect_retry=retries.append,
        )

        MainWindow.reconnect_live_session(window, "live-1")

        self.assertEqual(pending, [True])
        self.assertEqual(
            saved,
            [
                (
                    "live-1",
                    "기존 라이브 타임라인",
                    VodState.ANALYZING.value,
                )
            ],
        )
        self.assertEqual(
            states,
            [("live-1", VodState.ANALYZING.value)],
        )
        self.assertEqual(window._stale_live_sessions, ["live-1"])
        self.assertNotIn("live-1", window._live_reconnect_attempts)
        self.assertEqual(retries, [0])
        self.assertTrue(any("기존 자막과 타임라인은 유지" in item for item in messages))

    def test_replay_resolved_for_live_reanalysis_opens_merged_replay_tab(self):
        opened: list[str] = []
        linked: list[tuple[str, str]] = []
        applied: list[tuple[str, str]] = []

        class Database:
            @staticmethod
            def get_vod(vod_id: str):
                del vod_id
                return None

            @staticmethod
            def upsert_external_vod(**kwargs):
                return SimpleNamespace(
                    vod_id=kwargs["vod_id"],
                    streamer_id=7,
                )

            @staticmethod
            def link_live_session_to_replay(live_vod_id: str, replay_vod_id: str):
                linked.append((live_vod_id, replay_vod_id))

        window = SimpleNamespace(
            database=Database(),
            _pending_reanalysis_live_id="live-900",
            _pending_reanalysis_start=None,
            manual_link_input=SimpleNamespace(clear=lambda: None),
            status_label=SimpleNamespace(setText=lambda text: None),
            load_streamers=lambda: None,
            _select_streamer_tab=lambda streamer_id: None,
            load_vods=lambda: None,
            open_timeline=opened.append,
            _apply_linked_replay=lambda live_id, replay_id: applied.append(
                (live_id, replay_id)
            ),
            _manual_link_failed=lambda message: self.fail(message),
        )
        result = ResolvedVodLink(
            kind="vod",
            vod_id="777",
            channel_id="sample",
            streamer_name="샘플",
            title="완성된 다시보기",
            page_url="https://vod.sooplive.com/player/777",
            duration_text="07:00:00",
            published_text="오늘",
            thumbnail_url="",
        )

        MainWindow._manual_link_resolved(window, result)

        self.assertEqual(linked, [("live-900", "777")])
        self.assertEqual(applied, [("live-900", "777")])
        self.assertEqual(opened, ["777"])
        self.assertEqual(window._pending_reanalysis_start, ("live-900", "777"))

    def test_linked_reanalysis_runs_on_replay_after_live_work_migration(self):
        applied: list[tuple[str, str]] = []
        opened: list[str] = []
        analysis_calls: list[tuple[str, dict[str, object]]] = []
        live = SimpleNamespace(
            vod_id="live-900",
            streamer_id=7,
            source_kind="live",
            linked_vod_id="777",
            live_broadcast_no="900",
        )
        replay = SimpleNamespace(
            vod_id="777",
            streamer_id=7,
            source_kind="vod",
        )

        class Database:
            @staticmethod
            def get_vod(vod_id: str):
                return live if vod_id == live.vod_id else replay

            @staticmethod
            def list_live_sessions_for_broadcast(streamer_id: int, broadcast_no: str):
                self.assertEqual((streamer_id, broadcast_no), (7, "900"))
                return [live]

        window = SimpleNamespace(
            database=Database(),
            _active_jobs=lambda: [],
            _apply_linked_replay=lambda live_id, replay_id: applied.append(
                (live_id, replay_id)
            ),
            open_timeline=opened.append,
            _editor_tabs={"777": object()},
            start_analysis=lambda vod_id, **kwargs: analysis_calls.append(
                (vod_id, kwargs)
            ),
        )

        MainWindow._start_linked_replay_reanalysis(window, "live-900", "777")

        self.assertEqual(applied, [("live-900", "777")])
        self.assertEqual(opened, ["777"])
        self.assertEqual(analysis_calls[0][0], "777")
        self.assertNotIn("target_vod_id", analysis_calls[0][1])
        self.assertEqual(
            analysis_calls[0][1]["reusable_live_vods"],
            (live,),
        )

    def test_successful_live_reanalysis_converts_tab_to_replay_document(self):
        saved: list[tuple[str, str, str]] = []
        states: list[tuple[str, str]] = []
        revisions: list[tuple[str, str, str]] = []
        replacements: list[tuple[str, str, str]] = []
        feedback_drafts: list[tuple[str, str]] = []

        class Database:
            @staticmethod
            def get_timeline(vod_id: str):
                if vod_id == "777":
                    return SimpleNamespace(text="기존 다시보기 초안")
                return SimpleNamespace(text="기존 라이브 분석본")

            @staticmethod
            def create_timeline_revision(vod_id: str, text: str, reason: str):
                revisions.append((vod_id, text, reason))

            @staticmethod
            def save_timeline(vod_id: str, text: str, state: str):
                saved.append((vod_id, text, state))

            @staticmethod
            def set_vod_state(vod_id: str, state: str):
                states.append((vod_id, state))

            @staticmethod
            def remove_analysis_queue(vod_id: str):
                del vod_id

        live_editor = SimpleNamespace(text=lambda: "기존 라이브 분석본")
        replay_editor = SimpleNamespace(
            status_label=SimpleNamespace(setText=lambda text: None)
        )
        window = SimpleNamespace(
            database=Database(),
            _analysis_source_ids={"live-900": "777"},
            _analysis_previous_states={
                "live-900": (VodState.NEW.value, VodState.READY.value)
            },
            _editor_tabs={"live-900": live_editor},
            _replace_live_tab_with_replay=lambda live_id, replay_id, document: (
                replacements.append((live_id, replay_id, document))
                or replay_editor
            ),
            _refresh_editor_cache_state=lambda vod_id: None,
            _save_review_feedback_draft=lambda vod_id, document: (
                feedback_drafts.append((vod_id, document))
            ),
            status_label=SimpleNamespace(setText=lambda text: None),
            load_vods=lambda: None,
        )

        with patch(
            "soop_timeline.ui.main_window.has_pending_timeline_finalization",
            return_value=False,
        ):
            MainWindow._analysis_succeeded(
                window,
                "live-900",
                "전체 다시보기 분석 결과",
            )

        self.assertEqual(
            saved,
            [
                (
                    "777",
                    "전체 다시보기 분석 결과",
                    VodState.REVIEW.value,
                )
            ],
        )
        self.assertNotIn(
            ("live-900", "전체 다시보기 분석 결과", VodState.REVIEW.value),
            saved,
        )
        self.assertIn(("777", VodState.REVIEW.value), states)
        self.assertIn(("live-900", VodState.READY.value), states)
        self.assertIn(
            (
                "777",
                "기존 라이브 분석본",
                "라이브 분석본 · 전체 다시보기 재분석 전",
            ),
            revisions,
        )
        self.assertEqual(
            replacements,
            [("live-900", "777", "전체 다시보기 분석 결과")],
        )
        self.assertEqual(
            feedback_drafts,
            [("777", "전체 다시보기 분석 결과")],
        )


if __name__ == "__main__":
    unittest.main()
