import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.main import (
    _run_whisper_smoke_test,
    acquire_single_instance_lock,
    option_value,
)


class FakeApp:
    def __init__(self):
        self.exit_code = None

    def exit(self, code):
        self.exit_code = code


class MainTests(unittest.TestCase):
    def test_option_value_returns_following_argument(self):
        self.assertEqual(
            option_value(["main.py", "--open-vod", "200312857"], "--open-vod"),
            "200312857",
        )

    def test_option_value_handles_missing_value(self):
        self.assertEqual(option_value(["main.py", "--open-vod"], "--open-vod"), "")
        self.assertEqual(option_value(["main.py"], "--open-vod"), "")

    def test_single_instance_lock_rejects_second_owner(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = str(Path(temp_dir) / "app.lock")
            first = acquire_single_instance_lock(lock_path)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(acquire_single_instance_lock(lock_path))
            finally:
                first.unlock()

            replacement = acquire_single_instance_lock(lock_path)
            self.assertIsNotNone(replacement)
            replacement.unlock()

    def test_whisper_smoke_test_reports_model_load_result(self):
        target = (
            "soop_timeline.services.transcription."
            "FasterWhisperTranscriber._prepare_backend"
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"SOOP_TIMELINE_DATA_DIR": temp_dir}
        ):
            report = Path(temp_dir) / "whisper-smoke-result.txt"

            app = FakeApp()
            with patch(target, return_value=object()):
                _run_whisper_smoke_test(app)
            self.assertEqual(app.exit_code, 0)
            self.assertIn("loaded", report.read_text(encoding="utf-8"))

            app = FakeApp()
            with patch(
                target,
                side_effect=RuntimeError("Unable to open file 'model.bin'"),
            ):
                _run_whisper_smoke_test(app)
            self.assertEqual(app.exit_code, 2)
            self.assertIn("model.bin", report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
