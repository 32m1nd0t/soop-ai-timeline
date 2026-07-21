import unittest

from soop_timeline.services.transcript_export import transcript_to_srt, transcript_to_text
from soop_timeline.services.transcription import Transcript, TranscriptSegment


class TranscriptExportTests(unittest.TestCase):
    def test_exports_readable_text_and_srt(self):
        transcript = Transcript(
            model="large-v3-turbo",
            language="ko",
            duration_seconds=65.25,
            segments=[
                TranscriptSegment("s0", 1.25, 3.5, " 첫 번째 자막 "),
                TranscriptSegment("s1", 60.0, 65.25, "두 번째 자막"),
            ],
        )
        self.assertIn("00:00:01 첫 번째 자막", transcript_to_text(transcript))
        srt = transcript_to_srt(transcript)
        self.assertIn("00:00:01,250 --> 00:00:03,500", srt)
        self.assertIn("00:01:00,000 --> 00:01:05,250", srt)


if __name__ == "__main__":
    unittest.main()
