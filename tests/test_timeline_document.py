import unittest

from soop_timeline.services.timeline_document import (
    DEFAULT_TIMELINE_NOTICE,
    initial_timeline_document,
    prepend_timeline_notice,
    set_timeline_notice,
    timeline_notice,
)


class TimelineDocumentTests(unittest.TestCase):
    def setUp(self):
        set_timeline_notice(DEFAULT_TIMELINE_NOTICE)

    def tearDown(self):
        set_timeline_notice(DEFAULT_TIMELINE_NOTICE)

    def test_initial_document_prepends_default_notice(self):
        document = initial_timeline_document("테스트")

        self.assertEqual(
            document,
            f"{DEFAULT_TIMELINE_NOTICE}\n\n오늘의 콘텐츠: 테스트\n\n",
        )

    def test_custom_notice_is_used(self):
        set_timeline_notice("내 문구 한 줄")

        self.assertEqual(timeline_notice(), "내 문구 한 줄")
        self.assertEqual(
            prepend_timeline_notice("오늘의 콘텐츠: 테스트\n"),
            "내 문구 한 줄\n\n오늘의 콘텐츠: 테스트\n",
        )

    def test_empty_notice_disables_prefix(self):
        set_timeline_notice("")

        self.assertEqual(
            prepend_timeline_notice("오늘의 콘텐츠: 테스트\n"),
            "오늘의 콘텐츠: 테스트\n",
        )
        self.assertEqual(
            initial_timeline_document("테스트"),
            "오늘의 콘텐츠: 테스트\n\n",
        )

    def test_prepend_strips_leading_bom_and_blank_lines(self):
        set_timeline_notice("문구")

        self.assertEqual(
            prepend_timeline_notice("﻿\n\n오늘의 콘텐츠: 테스트\n"),
            "문구\n\n오늘의 콘텐츠: 테스트\n",
        )


if __name__ == "__main__":
    unittest.main()
