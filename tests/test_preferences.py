import unittest

from soop_timeline.services.preferences import (
    estimated_live_calls,
    live_ai_mode,
    normalized_cache_retention,
    normalized_discovery_interval,
    normalize_live_ai_mode,
    setting_enabled,
)


class PreferencesTests(unittest.TestCase):
    def test_live_mode_defaults_to_saving_and_estimates_calls(self):
        self.assertEqual(normalize_live_ai_mode("unknown"), "saving")
        self.assertEqual(live_ai_mode("saving").interval_seconds, 15 * 60)
        self.assertEqual(estimated_live_calls(6 * 3_600, "saving"), 25)
        self.assertGreater(
            estimated_live_calls(6 * 3_600, "frequent"),
            estimated_live_calls(6 * 3_600, "saving"),
        )

    def test_stored_preferences_are_normalized(self):
        self.assertEqual(normalized_discovery_interval("30"), 30)
        self.assertEqual(normalized_discovery_interval("bad"), 180)
        self.assertEqual(normalized_cache_retention("90"), 90)
        self.assertEqual(normalized_cache_retention("365"), 0)
        self.assertTrue(setting_enabled("1"))
        self.assertFalse(setting_enabled("false"))


if __name__ == "__main__":
    unittest.main()
