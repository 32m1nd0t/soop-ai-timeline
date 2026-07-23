import unittest
from types import SimpleNamespace

from soop_timeline.models import VodState
from soop_timeline.services.timeline_document import ensure_ai_timeline_notice
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
    def test_opening_existing_completed_timeline_does_not_change_state(self):
        existing = ensure_ai_timeline_notice("완료된 내용")
        database = _TimelineDatabase(state=VodState.READY.value, text=existing)
        window = SimpleNamespace(
            database=database,
            analyzer=SimpleNamespace(initial_document=lambda vod: "새 문서"),
        )

        text = MainWindow._load_or_create_timeline_text(window, database.vod)

        self.assertEqual(text, existing)
        self.assertEqual(database.saved, [])
        self.assertEqual(database.state_changes, [])

    def test_opening_older_timeline_adds_notice_without_reopening_review(self):
        database = _TimelineDatabase(state=VodState.READY.value, text="완료된 내용")
        window = SimpleNamespace(
            database=database,
            analyzer=SimpleNamespace(initial_document=lambda vod: "새 문서"),
        )

        text = MainWindow._load_or_create_timeline_text(window, database.vod)

        expected = ensure_ai_timeline_notice("완료된 내용")
        self.assertEqual(text, expected)
        self.assertEqual(
            database.saved,
            [("vod-1", expected, VodState.READY.value)],
        )
        self.assertEqual(database.state_changes, [])

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


if __name__ == "__main__":
    unittest.main()
