import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt

from soop_timeline.ui import timeline_editor
from soop_timeline.ui.timeline_editor import TimelineTextEdit


class TimelineEditorFocusTests(unittest.TestCase):
    def test_native_focus_restore_targets_qt_top_level_window(self):
        set_focus = Mock()
        fake_ctypes = SimpleNamespace(
            windll=SimpleNamespace(user32=SimpleNamespace(SetFocus=set_focus))
        )

        class FakeEditor:
            def __init__(self):
                self.focus_reasons = []

            def setFocus(self, reason):
                self.focus_reasons.append(reason)

            def window(self):
                return SimpleNamespace(winId=lambda: 4321)

        editor = FakeEditor()
        with (
            patch.object(timeline_editor.sys, "platform", "win32"),
            patch.object(timeline_editor, "ctypes", fake_ctypes),
        ):
            TimelineTextEdit._restore_native_keyboard_focus(editor)

        set_focus.assert_called_once_with(4321)
        self.assertEqual(
            editor.focus_reasons,
            [Qt.FocusReason.MouseFocusReason, Qt.FocusReason.MouseFocusReason],
        )


if __name__ == "__main__":
    unittest.main()
