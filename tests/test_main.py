import tempfile
import unittest
from pathlib import Path

from soop_timeline.main import acquire_single_instance_lock, option_value


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


if __name__ == "__main__":
    unittest.main()
