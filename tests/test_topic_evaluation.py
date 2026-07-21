import unittest

from soop_timeline.services.gemini_timeline import TimelineEntry, entries_from_payload
from soop_timeline.services.topic_evaluation import evaluate_topic_boundaries
from soop_timeline.services.transcription import TranscriptSegment


class TopicEvaluationTests(unittest.TestCase):
    def test_continue_decisions_do_not_create_timeline_rows(self):
        lookup = {
            "s0": TranscriptSegment("s0", 10, 12, "계속"),
            "s1": TranscriptSegment("s1", 40, 42, "새 주제"),
        }
        entries = entries_from_payload(
            {
                "entries": [
                    {"segment_id": "s0", "decision": "continue", "topic_key": "게임", "summary": "게임 이야기 계속"},
                    {"segment_id": "s1", "decision": "new", "topic_key": "꿈", "summary": "꿈 이야기 시작"},
                ]
            },
            lookup,
        )
        self.assertEqual([entry.segment_id for entry in entries], ["s1"])

    def test_boundary_score_matches_with_tolerance(self):
        predicted = [TimelineEntry("s0", 15, "첫 주제"), TimelineEntry("s1", 105, "둘째 주제")]
        score = evaluate_topic_boundaries(predicted, [10, 100, 300], tolerance_seconds=10)
        self.assertEqual(score.matched, 2)
        self.assertAlmostEqual(score.precision, 1.0)
        self.assertAlmostEqual(score.recall, 2 / 3)


if __name__ == "__main__":
    unittest.main()
