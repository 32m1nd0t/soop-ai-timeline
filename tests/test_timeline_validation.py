import unittest

from soop_timeline.services.timeline_validation import parse_duration_text, validate_timeline


class TimelineValidationTests(unittest.TestCase):
    def test_detects_duplicate_order_gap_and_range(self):
        document = (
            "오늘의 콘텐츠: 테스트\n\n"
            "00:01:00 첫 주제\n"
            "00:01:00 중복\n"
            "00:00:30 역순\n"
            "02:00:00 큰 간격\n"
        )
        kinds = {
            issue.kind for issue in validate_timeline(document, duration_seconds=3600)
        }
        self.assertTrue({"duplicate", "order", "gap", "range"}.issubset(kinds))

    def test_valid_document_passes(self):
        issues = validate_timeline(
            "오늘의 콘텐츠: 테스트\n\n00:00:10 시작\n00:20:00 다음 주제\n",
            duration_seconds=1800,
        )
        self.assertEqual(issues, [])

    def test_duration_text_supports_hours(self):
        self.assertEqual(parse_duration_text("3:28:04"), 12_484)


if __name__ == "__main__":
    unittest.main()
