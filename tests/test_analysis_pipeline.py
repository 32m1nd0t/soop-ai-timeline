import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.models import Vod
from soop_timeline.services.analyzer import AnalyzerConfig, LocalWhisperGeminiAnalyzer
from soop_timeline.services.gemini_timeline import (
    GeneratedTimeline,
    TimelineEntry,
    build_chunk_prompt,
    build_final_prompt,
    deduplicate_entries,
    split_transcript,
)
from soop_timeline.services.transcription import Transcript, TranscriptSegment
from soop_timeline.services.vod_stream import VodAudioPart, VodAudioSource
from soop_timeline.services.live_stream import LiveAudioSource


def sample_vod() -> Vod:
    return Vod(
        vod_id="123",
        streamer_id=1,
        channel_id="sample",
        streamer_name="샘플",
        title="테스트 방송",
        url="https://vod.sooplive.com/player/123",
        duration_text="1:00:00",
        published_text="방금 전",
        thumbnail_url="",
        state="new",
        discovered_at="",
        updated_at="",
    )


def sample_transcript() -> Transcript:
    return Transcript(
        model="large-v3-turbo",
        language="ko",
        duration_seconds=3_000,
        segments=[
            TranscriptSegment("s000000", 0, 15, "방송을 시작합니다"),
            TranscriptSegment("s000001", 2_600, 2_620, "꿈 이야기를 합니다"),
            TranscriptSegment("s000002", 2_800, 2_820, "게임을 시작합니다"),
        ],
    )


class AnalysisPipelineTests(unittest.TestCase):
    def test_transcript_is_split_with_overlap(self):
        windows = split_transcript(
            sample_transcript().segments,
            window_seconds=2_700,
            overlap_seconds=120,
        )
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0][-1].segment_id, "s000001")
        self.assertEqual(windows[1][0].segment_id, "s000001")

    def test_duplicate_entries_are_removed(self):
        entries = [
            TimelineEntry("s1", 10, "이상한 꿈을 꾼 이야기"),
            TimelineEntry("s2", 20, "이상한 꿈을 꿨던 이야기"),
            TimelineEntry("s3", 300, "게임을 시작함"),
        ]
        result = deduplicate_entries(entries)
        self.assertEqual([entry.segment_id for entry in result], ["s1", "s3"])

    def test_document_uses_fixed_timestamps(self):
        document = GeneratedTimeline(
            "테스트 콘텐츠",
            [TimelineEntry("s1", 3_725, "시청자와 꿈 이야기")],
        ).to_document()
        self.assertEqual(
            document,
            "오늘의 콘텐츠: 테스트 콘텐츠\n\n01:02:05 시청자와 꿈 이야기\n",
        )

    def test_gemini_prompts_require_concise_title_style(self):
        chunk_prompt = build_chunk_prompt(sample_vod(), sample_transcript().segments)
        final_prompt = build_final_prompt(
            sample_vod(),
            ["게임 합방"],
            [TimelineEntry("s000000", 0, "방송을 시작합니다.")],
            sample_transcript().segments,
        )

        for prompt in (chunk_prompt, final_prompt):
            self.assertIn("타임라인 소제목", prompt)
            self.assertIn("존댓말 종결어미와 마침표를 사용하지 않습니다", prompt)
            self.assertIn("여름·겨울 여행 환경과 선호 비교", prompt)
        self.assertIn("문체 규칙에 맞게 반드시 다시 작성", final_prompt)

    def test_topic_prompts_keep_previous_context_and_merge_subtopics(self):
        previous = [TimelineEntry("old", 100, "와우 시절과 현재 선호 비교")]
        chunk_prompt = build_chunk_prompt(
            sample_vod(),
            sample_transcript().segments,
            previous_entries=previous,
            granularity="broad",
        )
        final_prompt = build_final_prompt(
            sample_vod(),
            ["와우 토크"],
            [
                TimelineEntry("s000001", 2_600, "와우 시절 추억"),
                TimelineEntry("s000002", 2_800, "현재 게임 선호 이유"),
            ],
            sample_transcript().segments,
            granularity="broad",
        )

        self.assertIn("직전까지 확인된 주제", chunk_prompt)
        self.assertIn("와우 시절과 현재 선호 비교", chunk_prompt)
        self.assertIn("일정 시간마다 자막을 요약하는 작업이 아닙니다", chunk_prompt)
        self.assertIn("가장 이른 후보의 segment_id", final_prompt)
        self.assertIn("후보 주변 원문 근거", final_prompt)

    def test_gemini_emits_growing_timeline_previews(self):
        from soop_timeline.services.gemini_timeline import GeminiTimelineGenerator

        payloads = [
            {
                "content_title": "방송 시작",
                "entries": [
                    {"segment_id": "s000000", "summary": "방송을 시작함"},
                ],
            },
            {
                "content_title": "꿈과 게임",
                "entries": [
                    {"segment_id": "s000001", "summary": "꿈 이야기를 함"},
                    {"segment_id": "s000002", "summary": "게임을 시작함"},
                ],
            },
            {
                "content_title": "테스트 콘텐츠",
                "entries": [
                    {"segment_id": "s000000", "summary": "방송을 시작함"},
                    {"segment_id": "s000001", "summary": "꿈 이야기를 함"},
                    {"segment_id": "s000002", "summary": "게임을 시작함"},
                ],
            },
        ]
        previews: list[tuple[str, str]] = []
        generator = GeminiTimelineGenerator("test-key")

        with patch.object(generator, "_request_json", side_effect=payloads):
            result = generator.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
                preview=lambda stage, text: previews.append((stage, text)),
            )

        self.assertEqual(len(previews), 3)
        self.assertTrue(all(stage == "timeline" for stage, _ in previews))
        self.assertLess(len(previews[0][1]), len(previews[-1][1]))
        self.assertEqual(result.content_title, "테스트 콘텐츠")

    def test_analyzer_reuses_cached_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "audio.wav"
            source.write_bytes(b"test audio")
            transcribe_calls = []

            class FakeTranscriber:
                def transcribe(self, *args, **kwargs):
                    transcribe_calls.append(1)
                    return sample_transcript()

            class FakeGenerator:
                def generate(self, *args, **kwargs):
                    return GeneratedTimeline(
                        "테스트",
                        [TimelineEntry("s000000", 0, "방송 시작")],
                    )

            analyzer = LocalWhisperGeminiAnalyzer(
                AnalyzerConfig(gemini_api_key="test"),
                transcriber_factory=lambda model, device: FakeTranscriber(),
                generator_factory=lambda key, model: FakeGenerator(),
            )
            with patch(
                "soop_timeline.services.analyzer.analysis_data_dir",
                return_value=root,
            ):
                first = analyzer.analyze_media(
                    sample_vod(), source, lambda *args: None, lambda: False
                )
                second = analyzer.analyze_media(
                    sample_vod(), source, lambda *args: None, lambda: False
                )

            self.assertEqual(first, second)
            self.assertEqual(len(transcribe_calls), 1)

    def test_vod_analysis_uses_stream_transcriber_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stream_calls = []
            source = VodAudioSource(
                "123",
                3_000,
                (VodAudioPart(1, 3_000, "https://vod-a.sooplive.com/audio.m3u8"),),
            )

            class FakeTranscriber:
                def transcribe_stream(self, received, **kwargs):
                    stream_calls.append(received)
                    return sample_transcript()

            class FakeGenerator:
                def generate(self, *args, **kwargs):
                    return GeneratedTimeline(
                        "테스트",
                        [TimelineEntry("s000000", 0, "방송 시작")],
                    )

            analyzer = LocalWhisperGeminiAnalyzer(
                AnalyzerConfig(gemini_api_key="test"),
                transcriber_factory=lambda model, device: FakeTranscriber(),
                generator_factory=lambda key, model: FakeGenerator(),
            )
            with patch(
                "soop_timeline.services.analyzer.analysis_data_dir",
                return_value=root,
            ), patch(
                "soop_timeline.services.vod_stream.fetch_vod_audio_source",
                return_value=source,
            ):
                first = analyzer.analyze_vod(
                    sample_vod(), lambda *args: None, lambda: False
                )
                second = analyzer.analyze_vod(
                    sample_vod(), lambda *args: None, lambda: False
                )

            self.assertEqual(first, second)
            self.assertEqual(stream_calls, [source])

    def test_live_analysis_emits_incremental_and_final_timeline(self):
        live_vod = sample_vod()
        live_vod.source_kind = "live"
        live_vod.url = "https://play.sooplive.com/sample/98765"
        source = LiveAudioSource(
            kind="live",
            channel_id="sample",
            streamer_name="샘플",
            broadcast_no="98765",
            title="라이브 테스트",
            page_url=live_vod.url,
            runtime_seconds=3_600,
            stream_url="https://live-pcweb-kr-cdn-z02.sooplive.com/live/auth_playlist.m3u8?aid=x",
        )
        transcript = Transcript(
            model="large-v3-turbo",
            language="ko",
            duration_seconds=3_670,
            segments=[
                TranscriptSegment("s000000", 3_600, 3_610, "방송 시작"),
                TranscriptSegment("s000001", 3_665, 3_670, "게임 이야기"),
            ],
        )

        class FakeTranscriber:
            def transcribe_live(self, source, update, **kwargs):
                del source, kwargs
                update(transcript)
                return transcript

        class FakeGenerator:
            def summarize_live_window(
                self,
                vod,
                segments,
                cancelled,
                previous_entries=None,
            ):
                del vod, segments, cancelled, previous_entries
                return GeneratedTimeline(
                    "라이브 테스트",
                    [TimelineEntry("s000000", 3_600, "방송 시작")],
                )

            def finalize_live_entries(self, vod, titles, entries, segments, cancelled):
                del vod, titles, segments, cancelled
                return GeneratedTimeline("최종 라이브", entries)

        previews = []
        analyzer = LocalWhisperGeminiAnalyzer(
            AnalyzerConfig(gemini_api_key="test"),
            transcriber_factory=lambda model, device: FakeTranscriber(),
            generator_factory=lambda key, model: FakeGenerator(),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "soop_timeline.services.analyzer.analysis_data_dir",
            return_value=Path(directory),
        ):
            result = analyzer.analyze_live(
                live_vod,
                source,
                progress=lambda *args: None,
                stop_requested=lambda: False,
                preview=lambda stage, text: previews.append((stage, text)),
            )

        self.assertIn("01:00:00 방송 시작", result)
        self.assertEqual(result.splitlines()[0], "오늘의 콘텐츠: 최종 라이브")
        self.assertTrue(any(stage == "live_timeline" for stage, _ in previews))


if __name__ == "__main__":
    unittest.main()
