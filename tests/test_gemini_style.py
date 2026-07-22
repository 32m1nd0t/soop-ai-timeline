import unittest
from unittest.mock import patch

from soop_timeline.services.gemini_style import (
    GeminiTimelineStyler,
    build_style_prompt,
    parse_timeline_document,
)


SOURCE_DOCUMENT = (
    "오늘의 콘텐츠: 게임 합방\n"
    "\n"
    "00:01:02 게임을 시작하며 설정을 조절합니다.\n"
    "검수 메모는 그대로 유지\n"
    "01:02:03 어려운 장애물을 통과하기 위해 고군분투합니다.\n"
)


class GeminiStyleTests(unittest.TestCase):
    def test_parses_entries_and_preserves_fixed_timestamps(self):
        parsed = parse_timeline_document(SOURCE_DOCUMENT)
        self.assertEqual(len(parsed.entries), 2)
        self.assertEqual(parsed.entries[0].line_id, "line_0000")
        self.assertEqual(parsed.entries[1].timestamp, "01:02:03")

        result = parsed.rebuild(
            {
                "content_title": "협동 게임",
                "entries": [
                    {"line_id": "line_0000", "summary": "게임 시작과 설정 조정"},
                    {"line_id": "line_0001", "summary": "장애물 구간 공략"},
                ],
            }
        )

        self.assertEqual(
            result,
            "오늘의 콘텐츠: 협동 게임\n"
            "\n"
            "00:01:02 게임 시작과 설정 조정\n"
            "검수 메모는 그대로 유지\n"
            "01:02:03 장애물 구간 공략\n",
        )

    def test_rebuild_rejects_omitted_entries(self):
        parsed = parse_timeline_document(SOURCE_DOCUMENT)
        with self.assertRaisesRegex(RuntimeError, "1개 항목을 누락"):
            parsed.rebuild(
                {
                    "content_title": "협동 게임",
                    "entries": [
                        {"line_id": "line_0000", "summary": "게임 시작"},
                    ],
                }
            )

    def test_rebuild_cannot_change_existing_direct_quote(self):
        parsed = parse_timeline_document(
            '오늘의 콘텐츠: 토크\n\n00:00:10 "원래 실제 발언" 길게 설명합니다.\n'
        )
        result = parsed.rebuild(
            {
                "content_title": "토크",
                "entries": [
                    {
                        "line_id": "line_0000",
                        "summary": '"AI가 바꾼 발언" 짧은 메모',
                    }
                ],
            }
        )
        self.assertIn('00:00:10 "원래 실제 발언" 짧은 메모', result)

    def test_rebuild_keeps_original_line_if_ai_removes_quote(self):
        original = '00:00:10 "원래 실제 발언" 길게 설명합니다.'
        parsed = parse_timeline_document(f"오늘의 콘텐츠: 토크\n\n{original}\n")
        result = parsed.rebuild(
            {
                "content_title": "토크",
                "entries": [
                    {"line_id": "line_0000", "summary": "인용을 없앤 메모"}
                ],
            }
        )
        self.assertIn(original, result)

    def test_prompt_requires_dry_neutral_style_without_structure_changes(self):
        prompt = build_style_prompt(parse_timeline_document(SOURCE_DOCUMENT))
        self.assertIn("간결하고 자연스러운", prompt)
        self.assertIn("항목을 추가·삭제·병합·분할", prompt)
        self.assertIn("line_0000 | 00:01:02", prompt)
        self.assertIn("MBTI 검사 재진행", prompt)

    def test_styler_rewrites_document_without_a_network_call(self):
        styler = GeminiTimelineStyler("test-key")
        payload = {
            "content_title": "협동 게임",
            "entries": [
                {"line_id": "line_0000", "summary": "게임 시작과 설정 조정"},
                {"line_id": "line_0001", "summary": "장애물 구간 공략"},
            ],
        }
        with patch.object(styler, "_request_json", return_value=payload):
            result = styler.restyle(SOURCE_DOCUMENT)

        self.assertIn("00:01:02 게임 시작과 설정 조정", result)
        self.assertIn("01:02:03 장애물 구간 공략", result)


if __name__ == "__main__":
    unittest.main()
