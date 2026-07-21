import unittest

from soop_timeline.main import option_value


class MainTests(unittest.TestCase):
    def test_option_value_returns_following_argument(self):
        self.assertEqual(
            option_value(["main.py", "--open-vod", "200312857"], "--open-vod"),
            "200312857",
        )

    def test_option_value_handles_missing_value(self):
        self.assertEqual(option_value(["main.py", "--open-vod"], "--open-vod"), "")
        self.assertEqual(option_value(["main.py"], "--open-vod"), "")


if __name__ == "__main__":
    unittest.main()
