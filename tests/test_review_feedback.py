import unittest
from unittest.mock import patch

from soop_timeline.models import ReviewFeedbackExample, Vod
from soop_timeline.services.gemini_timeline import GeminiTimelineGenerator
from soop_timeline.services.review_feedback import (
    build_review_feedback_prompt,
    extract_review_feedback,
    review_feedback_fingerprint,
    select_review_feedback_examples,
)
from soop_timeline.services.transcription import Transcript, TranscriptSegment


def sample_vod(streamer_id: int = 7) -> Vod:
    return Vod(
        vod_id="vod-1",
        streamer_id=streamer_id,
        channel_id="sample",
        streamer_name="샘플",
        title="샘플 방송",
        url="https://vod.sooplive.com/player/vod-1",
        duration_text="1:00:00",
        published_text="오늘",
        thumbnail_url="",
        state="review",
        discovered_at="",
        updated_at="",
    )


def example(
    example_id: int,
    action: str,
    before: str,
    after: str,
    *,
    streamer_id: int = 7,
) -> ReviewFeedbackExample:
    return ReviewFeedbackExample(
        id=example_id,
        vod_id=f"vod-{example_id}",
        streamer_id=streamer_id,
        action=action,
        before_text=before,
        after_text=after,
        created_at="2026-08-02T00:00:00+00:00",
    )


class ReviewFeedbackTests(unittest.TestCase):
    def test_extracts_title_rewrite_delete_add_and_timestamp_edits(self):
        draft = (
            "오늘의 콘텐츠: 기존 제목\n\n"
            "00:00:10 긴 문장으로 방송을 시작함\n"
            "00:10:00 단순 후원 감사\n"
            "00:15:00 그대로 유지할 중간 항목\n"
            "00:20:00 첫 번째 게임 시작\n"
            "00:21:00 시간 수정 뒤 유지할 항목\n"
            "00:25:00 그대로 유지할 마지막 항목\n"
        )
        reviewed = (
            "오늘의 콘텐츠: 수정한 제목\n\n"
            "00:00:10 방송 시작\n"
            "00:15:00 그대로 유지할 중간 항목\n"
            "00:20:05 첫 번째 게임 시작\n"
            "00:21:00 시간 수정 뒤 유지할 항목\n"
            "00:22:00 중요한 공지\n"
            "00:25:00 그대로 유지할 마지막 항목\n"
        )

        edits = extract_review_feedback(draft, reviewed)

        self.assertEqual(
            [edit.action for edit in edits],
            ["title", "rewrite", "delete", "timestamp", "add"],
        )
        self.assertEqual(edits[2].after_text, "")
        self.assertIn("00:22:00 중요한 공지", edits[-1].after_text)

    def test_extracts_merge_and_split_when_timestamp_groups_are_replaced(self):
        draft = (
            "오늘의 콘텐츠: 테스트\n\n"
            "00:10:00 첫 설명\n"
            "00:20:00 두 번째 설명\n"
            "00:25:00 그대로 유지할 항목\n"
            "00:30:00 하나의 긴 주제\n"
        )
        reviewed = (
            "오늘의 콘텐츠: 테스트\n\n"
            "00:15:00 설명을 하나로 병합\n"
            "00:25:00 그대로 유지할 항목\n"
            "00:31:00 긴 주제 전반부\n"
            "00:32:00 긴 주제 후반부\n"
        )

        edits = extract_review_feedback(draft, reviewed)

        self.assertEqual([edit.action for edit in edits], ["merge", "split"])
        self.assertIn("00:10:00 첫 설명", edits[0].before_text)
        self.assertIn("00:32:00 긴 주제 후반부", edits[1].after_text)

    def test_selects_only_same_streamer_and_relevant_stage_examples(self):
        examples = [
            example(1, "delete", "00:10:00 후원 감사", ""),
            example(2, "rewrite", "00:20:00 게임을 시작함", "00:20:00 게임 시작"),
            example(3, "title", "기존 제목", "새 제목"),
            example(4, "delete", "00:30:00 후원 감사", "", streamer_id=99),
        ]

        selected = select_review_feedback_examples(
            examples,
            7,
            "chunk",
            "후원 감사 후 게임을 시작했다",
            max_examples=3,
        )

        self.assertEqual({item.id for item in selected}, {1, 2})
        self.assertNotIn(3, {item.id for item in selected})
        self.assertNotIn(4, {item.id for item in selected})

        title_prompt = build_review_feedback_prompt(
            examples,
            sample_vod(),
            "title",
        )
        self.assertIn("전체 제목 수정", title_prompt)
        self.assertIn("새 제목", title_prompt)
        self.assertNotIn("후원 감사", title_prompt)

    def test_fingerprint_changes_with_streamer_feedback(self):
        original = [example(1, "delete", "잡담", "")]
        changed = [*original, example(2, "rewrite", "긴 문장", "짧은 문장")]

        self.assertNotEqual(
            review_feedback_fingerprint(original, 7),
            review_feedback_fingerprint(changed, 7),
        )
        self.assertEqual(review_feedback_fingerprint(changed, 99), "")

    def test_generator_injects_only_stage_specific_feedback(self):
        feedback = [
            example(1, "delete", "00:10:00 단순 후원 감사", ""),
            example(2, "title", "기존 전체 제목", "수정한 전체 제목"),
            example(3, "rewrite", "00:20:00 방송을 시작함", "00:20:00 방송 시작"),
        ]
        transcript = Transcript(
            "large-v3-turbo",
            "ko",
            30.0,
            [
                TranscriptSegment(
                    "s000000",
                    10.0,
                    20.0,
                    "단순 후원 감사 후 방송을 시작했다",
                )
            ],
        )
        payloads = [
            {
                "content_title": "임시 제목",
                "entries": [
                    {
                        "segment_id": "s000000",
                        "decision": "new",
                        "topic_key": "방송 시작",
                        "summary": "방송 시작",
                        "quote": "",
                        "section_break_before": False,
                    }
                ],
            },
            {
                "entries": [
                    {
                        "segment_id": "s000000",
                        "decision": "new",
                        "topic_key": "방송 시작",
                        "summary": "방송 시작",
                        "quote": "",
                        "section_break_before": False,
                    }
                ]
            },
            {"content_title": "최종 제목"},
        ]
        calls: list[str] = []

        def request(prompt, _cancelled, **_kwargs):
            calls.append(prompt)
            return payloads[len(calls) - 1]

        generator = GeminiTimelineGenerator(
            "test-key",
            review_feedback_examples=feedback,
        )
        with patch.object(generator, "_request_json", side_effect=request):
            generator.generate(
                sample_vod(),
                transcript,
                progress=lambda *_args: None,
                cancelled=lambda: False,
            )

        self.assertEqual(len(calls), 3)
        self.assertIn("단순 후원 감사", calls[0])
        self.assertNotIn("수정한 전체 제목", calls[0])
        self.assertIn("방송을 시작함", calls[1])
        self.assertNotIn("수정한 전체 제목", calls[1])
        self.assertIn("수정한 전체 제목", calls[2])
        self.assertNotIn("단순 후원 감사", calls[2])


if __name__ == "__main__":
    unittest.main()
