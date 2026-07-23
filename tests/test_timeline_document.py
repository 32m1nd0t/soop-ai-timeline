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

    def test_legacy_notice_is_replaced_without_duplication(self):
        old_notice = (
            "본 타임라인은 AI로 작성되어 수동 작성본보다 정확도가 낮습니다.\n"
            "직접 작성을 원하시는 분이 계신다면 언제든 자리를 양보하겠습니다."
        )
        document = f"{old_notice}\n\n오늘의 콘텐츠: 테스트\n"

        updated = ensure_ai_timeline_notice(document)

        self.assertEqual(
            updated,
            f"{AI_TIMELINE_NOTICE}\n\n오늘의 콘텐츠: 테스트\n",
        )
        self.assertNotIn("자리를 양보", updated)

    def test_empty_document_still_has_editable_space_after_notice(self):
        self.assertEqual(
            ensure_ai_timeline_notice(""),
            f"{AI_TIMELINE_NOTICE}\n\n",
        )


if __name__ == "__main__":
    unittest.main()
