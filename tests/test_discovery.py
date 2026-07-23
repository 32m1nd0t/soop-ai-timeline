import unittest

from soop_timeline.services.discovery import (
    additional_vod_items,
    classify_discovery_result,
)


class DiscoveryTests(unittest.TestCase):
    def test_waits_for_page_instead_of_accepting_early_empty_list(self):
        result = {
            "items": [],
            "ready_state": "complete",
            "explicit_empty": False,
        }
        self.assertEqual(
            classify_discovery_result(result, 1_000, 15_000).action,
            "wait",
        )
        decision = classify_discovery_result(result, 15_000, 15_000)
        self.assertEqual(decision.action, "error")
        self.assertIn("구조", decision.message)

    def test_accepts_items_or_explicit_empty_state(self):
        item_result = {"items": [{"vod_id": "1"}], "ready_state": "complete"}
        empty_result = {
            "items": [],
            "ready_state": "complete",
            "explicit_empty": True,
        }
        self.assertEqual(classify_discovery_result(item_result, 0, 15_000).action, "success")
        self.assertEqual(classify_discovery_result(empty_result, 0, 15_000).action, "success")

    def test_detects_access_block(self):
        decision = classify_discovery_result(
            {"items": [], "blocked": True},
            0,
            15_000,
        )
        self.assertEqual(decision.action, "error")
        self.assertIn("차단", decision.message)

    def test_more_loading_skips_known_vods_and_keeps_page_order(self):
        items = [
            {"vod_id": "5"},
            {"vod_id": "4"},
            {"vod_id": "3"},
            {"vod_id": "2"},
        ]
        self.assertEqual(
            [
                item["vod_id"]
                for item in additional_vod_items(items, {"5", "4"}, limit=2)
            ],
            ["3", "2"],
        )


if __name__ == "__main__":
    unittest.main()
