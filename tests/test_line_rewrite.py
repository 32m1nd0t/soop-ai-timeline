from __future__ import annotations

import unittest

from soop_timeline.services.gemini_line_rewrite import (
    AITimelineLineRewriter,
    build_transcript_excerpt,
)
from soop_timeline.services.timeline_timestamp import (
    replace_timeline_line_at_position,
    timeline_line_at_position,
)
from soop_timeline.services.transcription import (
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)


class FakeProvider:
    def __init__(self, text: str):
        self._text = text
        self.available = True
        self.unavailable_reason = ""
        self.provider_id = "gemini"
        self.prompts: list[str] = []

    def request_json(self, prompt, schema, cancelled, purpose=""):
        self.prompts.append(prompt)
        return {"text": self._text}


def make_transcript() -> Transcript:
    return Transcript(
        model="test",
        language="ko",
        duration_seconds=2_000.0,
        segments=[
            TranscriptSegment("s0", 1_195.0, 1_199.0, "응 그건 그렇고"),
            TranscriptSegment(
                "s1", 1_205.0, 1_212.0, "어제 진짜 이상한 꿈을 꿨다니까"
            ),
            TranscriptSegment("s2", 1_215.0, 1_220.0, "꿈에서 회사를 갔는데"),
        ],
        words=(
            TranscriptWord(1_205.4, 1_205.8, "어제"),
            TranscriptWord(1_205.9, 1_206.2, "진짜"),
            TranscriptWord(1_206.3, 1_206.7, "이상한"),
            TranscriptWord(1_206.8, 1_207.3, "꿈을"),
        ),
    )


class TimelineLineAtPositionTests(unittest.TestCase):
    def test_finds_line_and_next_entry(self):
        document = (
            "오늘의 콘텐츠: 테스트\n"
            "\n"
            "0:20:00 어제 꿈 이야기\n"
            "0:25:00 다음 주제\n"
        )
        position = document.index("어제 꿈")
        hit = timeline_line_at_position(document, position)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.line, "0:20:00 어제 꿈 이야기")
        self.assertEqual(hit.seconds, 1_200)
        self.assertEqual(hit.next_seconds, 1_500)
        self.assertEqual(document[hit.start : hit.end], hit.line)

    def test_last_entry_has_no_next(self):
        document = "0:20:00 마지막 주제\n"
        hit = timeline_line_at_position(document, 10)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.next_seconds, -1)

    def test_non_timeline_line_returns_none(self):
        document = "오늘의 콘텐츠: 테스트\n0:20:00 주제\n"
        self.assertIsNone(timeline_line_at_position(document, 3))

    def test_replaces_selected_duplicate_line_only(self):
        duplicate = "0:20:00 같은 내용"
        document = f"{duplicate}\n0:21:00 중간\n{duplicate}\n"
        hit = timeline_line_at_position(document, document.rfind("같은 내용"))
        self.assertIsNotNone(hit)
        updated, changed = replace_timeline_line_at_position(
            document,
            hit.start,
            hit.line,
            "0:20:05 선택한 두 번째 줄",
        )
        self.assertTrue(changed)
        self.assertEqual(updated.count(duplicate), 1)
        self.assertTrue(updated.endswith("0:20:05 선택한 두 번째 줄\n"))


class LineRewriteTests(unittest.TestCase):
    def test_excerpt_covers_topic_span(self):
        excerpt, upper = build_transcript_excerpt(make_transcript(), 1_200, 1_500)
        self.assertIn("어제 진짜 이상한 꿈을", excerpt)
        self.assertEqual(upper, 1_500.0)

    def test_quote_mode_snaps_timestamp_to_spoken_word(self):
        provider = FakeProvider("어제 진짜 이상한 꿈을 꿨다니까")
        rewriter = AITimelineLineRewriter(provider)
        result = rewriter.rewrite(
            "quote",
            "0:20:00 어제 꾼 이상한 꿈 이야기",
            1_500,
            make_transcript(),
        )
        self.assertEqual(result, '00:20:05 "어제 진짜 이상한 꿈을 꿨다니까"')

    def test_quote_mode_rejects_fabricated_sentence(self):
        provider = FakeProvider("자막에 전혀 없는 창작 문장입니다")
        rewriter = AITimelineLineRewriter(provider)
        with self.assertRaises(RuntimeError):
            rewriter.rewrite(
                "quote",
                "0:20:00 어제 꾼 이상한 꿈 이야기",
                1_500,
                make_transcript(),
            )

    def test_quote_mode_rejects_sentence_stitched_across_a_long_pause(self):
        transcript = Transcript(
            model="test",
            language="ko",
            duration_seconds=100.0,
            segments=[
                TranscriptSegment("s0", 10.0, 12.0, "오늘은 사과"),
                TranscriptSegment("s1", 70.0, 72.0, "게임을 합니다"),
            ],
        )
        rewriter = AITimelineLineRewriter(
            FakeProvider("오늘은 사과 게임을 합니다")
        )
        with self.assertRaises(RuntimeError):
            rewriter.rewrite(
                "quote",
                "0:00:10 사과 게임 이야기",
                90,
                transcript,
            )

    def test_summary_mode_keeps_timestamp_and_normalizes(self):
        provider = FakeProvider("어제 꾼 이상한 꿈 이야기.")
        rewriter = AITimelineLineRewriter(provider)
        result = rewriter.rewrite(
            "summary",
            '0:20:00 "어제 진짜 이상한 꿈을 꿨다니까"',
            1_500,
            make_transcript(),
        )
        self.assertEqual(result, "0:20:00 어제 꾼 이상한 꿈 이야기")


if __name__ == "__main__":
    unittest.main()
