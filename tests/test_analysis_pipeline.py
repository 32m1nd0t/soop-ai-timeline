import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.models import Vod
from soop_timeline.services.analyzer import (
    AnalyzerConfig,
    LocalWhisperGeminiAnalyzer,
    build_whisper_prompt,
    load_cached_transcript,
    load_timeline_generation_state,
    save_timeline_generation_state,
)
from soop_timeline.services.gemini_timeline import (
    GeneratedTimeline,
    TimelineEntry,
    TimelineGenerationState,
    build_chunk_prompt,
    build_final_prompt,
    deduplicate_entries,
    entries_from_payload,
    format_entry_text,
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
    def test_timeline_checkpoint_round_trip_and_key_validation(self):
        state = TimelineGenerationState(
            "key-1",
            2,
            ["제목"],
            [TimelineEntry("s1", 10, '"직접 인용" — 메모', "주제", "new", "직접 인용")],
            stage="final_pending",
            last_error="quota",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "timeline.partial.json"
            save_timeline_generation_state(path, state)
            restored = load_timeline_generation_state(path, "key-1")
            self.assertEqual(restored.to_dict(), state.to_dict())
            self.assertEqual(restored.entries[0].quote, "직접 인용")
            self.assertIsNone(load_timeline_generation_state(path, "other-key"))

    def test_quote_first_entries_keep_quote_and_summary_separate(self):
        lookup = {
            segment.segment_id: segment for segment in sample_transcript().segments
        }
        payload = {
            "entries": [
                # Quote-only: summary stays empty and the display is just the quote.
                {
                    "segment_id": "s000001",
                    "decision": "new",
                    "topic_key": "꿈",
                    "quote": "어제 진짜 이상한 꿈 꿨거든요",
                    "summary": "",
                },
                # Quote plus genuine extra context.
                {
                    "segment_id": "s000002",
                    "decision": "new",
                    "topic_key": "게임",
                    "quote": "자 이제 시작합니다",
                    "summary": "신작 점프맵 첫 도전",
                },
            ]
        }
        entries = entries_from_payload(payload, lookup)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].quote, "어제 진짜 이상한 꿈 꿨거든요")
        self.assertEqual(entries[0].summary, "")
        self.assertEqual(
            format_entry_text(entries[0]), '"어제 진짜 이상한 꿈 꿨거든요"'
        )
        self.assertEqual(
            format_entry_text(entries[1]), '"자 이제 시작합니다" 신작 점프맵 첫 도전'
        )

    def test_entry_display_uses_plain_summary_without_quote(self):
        entry = TimelineEntry("s1", 10, "루딘한테 극딜하는 혜지")
        self.assertEqual(format_entry_text(entry), "루딘한테 극딜하는 혜지")
        # A summary that merely repeats the quote is not shown twice.
        duplicated = TimelineEntry("s2", 20, '"안녕하세요"', quote="안녕하세요")
        self.assertEqual(format_entry_text(duplicated), '"안녕하세요"')

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
        self.assertIn("문체 규칙에 맞게 간결하고 자연스럽게", final_prompt)

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

    def test_prompts_treat_transcript_as_untrusted_and_use_glossary(self):
        vod = sample_vod()
        vod.streamer_glossary = "마이곰이\n와우 = 월드 오브 워크래프트"
        chunk_prompt = build_chunk_prompt(vod, sample_transcript().segments)
        final_prompt = build_final_prompt(
            vod,
            ["와우 토크"],
            [TimelineEntry("s000000", 0, "와우 이야기")],
            sample_transcript().segments,
        )
        for prompt in (chunk_prompt, final_prompt):
            self.assertIn("마이곰이", prompt)
            self.assertIn("비신뢰 데이터", prompt)
            self.assertIn("<glossary>", prompt)
        self.assertIn("마이곰이", build_whisper_prompt(vod, live=False))

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

    def test_gemini_final_failure_keeps_checkpoint_and_resumes_final_only(self):
        from soop_timeline.services.gemini_timeline import GeminiTimelineGenerator

        window_payloads = [
            {
                "content_title": "방송 시작",
                "entries": [
                    {
                        "segment_id": "s000000",
                        "decision": "new",
                        "topic_key": "시작",
                        "summary": "방송 시작",
                    }
                ],
            },
            {
                "content_title": "꿈과 게임",
                "entries": [
                    {
                        "segment_id": "s000001",
                        "decision": "new",
                        "topic_key": "꿈",
                        "summary": "꿈 이야기",
                    },
                    {
                        "segment_id": "s000002",
                        "decision": "new",
                        "topic_key": "게임",
                        "summary": "게임 시작",
                    },
                ],
            },
        ]
        checkpoints: list[TimelineGenerationState] = []
        generator = GeminiTimelineGenerator("test-key")
        with patch.object(
            generator,
            "_request_json",
            side_effect=[*window_payloads, RuntimeError("quota per day exceeded")],
        ):
            draft = generator.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
                checkpoint_key="checkpoint-key",
                checkpoint=checkpoints.append,
            )
        self.assertTrue(generator.last_warning)
        self.assertEqual(checkpoints[-1].stage, "final_pending")
        self.assertEqual(checkpoints[-1].completed_windows, 2)
        self.assertGreaterEqual(len(draft.entries), 2)

        final_payload = {
            "content_title": "최종 콘텐츠",
            "entries": [
                {
                    "segment_id": "s000000",
                    "decision": "new",
                    "topic_key": "시작",
                    "summary": "방송 시작",
                },
                {
                    "segment_id": "s000002",
                    "decision": "new",
                    "topic_key": "게임",
                    "summary": "게임 시작",
                },
            ],
        }
        resumed = GeminiTimelineGenerator("test-key")
        with patch.object(resumed, "_request_json", return_value=final_payload) as request:
            result = resumed.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
                checkpoint_key="checkpoint-key",
                resume_state=checkpoints[-1],
            )
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result.content_title, "최종 콘텐츠")

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
            recovered = load_cached_transcript(live_vod)

        self.assertIn("01:00:00 방송 시작", result)
        self.assertEqual(len(recovered.segments), 2)
        self.assertEqual(result.splitlines()[0], "오늘의 콘텐츠: 최종 라이브")
        self.assertTrue(any(stage == "live_timeline" for stage, _ in previews))


if __name__ == "__main__":
    unittest.main()
