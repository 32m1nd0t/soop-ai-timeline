import unittest

from soop_timeline.services.timeline_document import (
    AI_TIMELINE_NOTICE,
    ensure_ai_timeline_notice,
    has_ai_timeline_notice,
    initial_timeline_document,
)


class TimelineDocumentTests(unittest.TestCase):
    def test_notice_is_added_above_content_title(self):
        document = ensure_ai_timeline_notice(
            "오늘의 콘텐츠: 테스트\n\n00:00:10 방송 시작\n"
        )

        self.assertEqual(
            document,
            f"{AI_TIMELINE_NOTICE}\n\n"
            "오늘의 콘텐츠: 테스트\n\n"
            "00:00:10 방송 시작\n",
        )
        self.assertTrue(has_ai_timeline_notice(document))

    def test_adding_notice_is_idempotent(self):
        document = initial_timeline_document("테스트")

        self.assertEqual(ensure_ai_timeline_notice(document), document)
        self.assertEqual(document.count(AI_TIMELINE_NOTICE), 1)

    def test_empty_document_still_has_editable_space_after_notice(self):
        self.assertEqual(
            ensure_ai_timeline_notice(""),
            f"{AI_TIMELINE_NOTICE}\n\n",
        )


if __name__ == "__main__":
    unittest.main()
