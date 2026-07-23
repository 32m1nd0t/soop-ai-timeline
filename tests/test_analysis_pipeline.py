import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from soop_timeline.models import Vod
from soop_timeline.services.analyzer import (
    AnalyzerConfig,
    LIVE_TRANSCRIPT_FILENAME,
    LIVE_TRANSCRIPT_JOURNAL_FILENAME,
    LocalWhisperGeminiAnalyzer,
    _LiveTranscriptJournal,
    build_whisper_prompt,
    load_cached_transcript,
    load_timeline_generation_state,
    save_timeline_generation_state,
)
from soop_timeline.services.gemini_timeline import (
    FINAL_TIMELINE_SCHEMA,
    OVERALL_SUMMARY_SCHEMA,
    GeneratedTimeline,
    TimelineEntry,
    TimelineGenerationState,
    build_overall_summary,
    build_overall_summary_prompt,
    build_chunk_prompt,
    build_final_prompt,
    deduplicate_entries,
    enforce_broadcast_ending_quotes,
    entries_from_payload,
    find_phrase_start_time,
    format_entry_text,
    preserve_broadcast_ending_quotes,
    resolve_overall_summary,
    snap_entries_to_words,
    split_transcript,
    validate_and_snap_quotes,
)
from soop_timeline.services.timeline_document import AI_TIMELINE_NOTICE
from soop_timeline.services.transcription import (
    AnalysisCancelled,
    LiveTranscriptUpdate,
    Transcript,
    TranscriptSegment,
    TranscriptWord,
)
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
                    "quote": "꿈 이야기를 합니다",
                    "summary": "",
                },
                # Quote plus genuine extra context.
                {
                    "segment_id": "s000002",
                    "decision": "new",
                    "topic_key": "게임",
                    "quote": "게임을 시작합니다",
                    "summary": "신작 점프맵 첫 도전",
                },
            ]
        }
        entries = entries_from_payload(payload, lookup)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].quote, "꿈 이야기를 합니다")
        self.assertEqual(entries[0].summary, "")
        self.assertEqual(
            format_entry_text(entries[0]), '"꿈 이야기를 합니다"'
        )
        self.assertEqual(
            format_entry_text(entries[1]), '"게임을 시작합니다" 신작 점프맵 첫 도전'
        )

    def test_fabricated_quote_is_downgraded_to_transcript_text(self):
        transcript = sample_transcript()
        payload = {
            "entries": [
                {
                    "segment_id": "s000001",
                    "decision": "new",
                    "topic_key": "꿈",
                    "quote": "자막에 전혀 없는 창작 문장입니다",
                    "summary": "",
                }
            ]
        }
        entries = entries_from_payload(
            payload,
            {segment.segment_id: segment for segment in transcript.segments},
        )
        self.assertEqual(entries[0].quote, "")
        self.assertEqual(entries[0].summary, "꿈 이야기를 합니다")
        self.assertNotIn('"', format_entry_text(entries[0]))

    def test_quote_match_never_accepts_only_a_four_character_prefix(self):
        words = [TranscriptWord(10.0, 10.5, "오늘방송은 완전히 다른 내용")]
        self.assertIsNone(
            find_phrase_start_time("오늘방송에서 신작 게임을 시작합니다", words)
        )

    def test_quote_match_tolerates_one_whisper_spelling_difference(self):
        words = [
            TranscriptWord(10.0, 11.0, "어제 진짜 이상한 꿈을 꿨다니까")
        ]
        self.assertEqual(
            find_phrase_start_time(
                "어제 진짜 이상한 꿈을 꿧다니까",
                words,
                reference_time=10.0,
            ),
            10.0,
        )

    def test_quote_cannot_join_transcript_segments_separated_by_a_long_gap(self):
        segments = [
            TranscriptSegment("s1", 10.0, 12.0, "오늘은 사과"),
            TranscriptSegment("s2", 70.0, 72.0, "게임을 합니다"),
        ]
        entry = TimelineEntry(
            "s1",
            10.0,
            "사과 게임 이야기",
            quote="오늘은 사과 게임을 합니다",
        )
        result = validate_and_snap_quotes([entry], segments)[0]
        self.assertEqual(result.quote, "")
        self.assertEqual(result.summary, "사과 게임 이야기")

    def test_snap_moves_quoted_entry_to_the_spoken_word(self):
        words = (
            TranscriptWord(1200.0, 1200.3, "응"),
            TranscriptWord(1200.5, 1200.9, "그건"),
            TranscriptWord(1201.0, 1201.4, "그렇고"),
            TranscriptWord(1205.4, 1205.8, "어제"),
            TranscriptWord(1205.9, 1206.2, "진짜"),
            TranscriptWord(1206.3, 1206.7, "이상한"),
            TranscriptWord(1206.8, 1207.1, "꿈"),
        )
        # The chosen segment starts at 1200.0 but the topic opens 5.4s later.
        entry = TimelineEntry(
            "s1", 1200.0, '"어제 진짜 이상한 꿈"', quote="어제 진짜 이상한 꿈"
        )
        snapped = snap_entries_to_words([entry], words)
        self.assertEqual(len(snapped), 1)
        self.assertAlmostEqual(snapped[0].start, 1205.4, places=3)

    def test_snap_falls_back_safely(self):
        words = (TranscriptWord(10.0, 10.4, "안녕"),)
        # No word timings at all -> unchanged.
        plain = TimelineEntry("s1", 100.0, "요약만", quote="")
        self.assertEqual(snap_entries_to_words([plain], ())[0].start, 100.0)
        # Quote not present near the entry -> unchanged.
        mismatch = TimelineEntry("s2", 10.0, '"없는 말"', quote="전혀 다른 말입니다")
        self.assertEqual(snap_entries_to_words([mismatch], words)[0].start, 10.0)
        # No quote -> unchanged even with word timings available.
        no_quote = TimelineEntry("s3", 10.0, "요약", quote="")
        self.assertEqual(snap_entries_to_words([no_quote], words)[0].start, 10.0)

    def test_entry_display_uses_plain_summary_without_quote(self):
        entry = TimelineEntry("s1", 10, "루딘한테 극딜하는 혜지")
        self.assertEqual(format_entry_text(entry), "루딘한테 극딜하는 혜지")
        # A summary that merely repeats the quote is not shown twice.
        duplicated = TimelineEntry("s2", 20, '"안녕하세요"', quote="안녕하세요")
        self.assertEqual(format_entry_text(duplicated), '"안녕하세요"')

    def test_current_broadcast_ending_is_forced_to_a_transcript_quote(self):
        segment = TranscriptSegment(
            "ending",
            7_200,
            7_208,
            "자 오늘 방송은 여기까지 하겠습니다. 다들 고마워요.",
        )
        payload = {
            "entries": [
                {
                    "segment_id": "ending",
                    "decision": "new",
                    "topic_key": "방송 마무리",
                    "summary": "방송 종료 및 마지막 인사",
                    "quote": "",
                }
            ]
        }

        result = entries_from_payload(payload, {segment.segment_id: segment})

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].summary, "")
        self.assertEqual(
            result[0].quote,
            "자 오늘 방송은 여기까지 하겠습니다.",
        )
        self.assertEqual(
            format_entry_text(result[0]),
            '"자 오늘 방송은 여기까지 하겠습니다."',
        )

    def test_negated_or_game_end_is_not_forced_to_a_broadcast_quote(self):
        segments = [
            TranscriptSegment(
                "not-ending",
                100,
                108,
                "오늘 방송은 아직 종료할 생각은 없어요.",
            ),
            TranscriptSegment(
                "game-ending",
                200,
                208,
                "이제 게임을 종료할게요.",
            ),
        ]
        entries = [
            TimelineEntry(
                "not-ending",
                100,
                "방송 종료를 부정함",
                "방송 종료",
            ),
            TimelineEntry(
                "game-ending",
                200,
                "게임 종료",
                "게임 종료",
            ),
        ]

        result = enforce_broadcast_ending_quotes(entries, segments)

        self.assertEqual([entry.quote for entry in result], ["", ""])
        self.assertEqual(
            [entry.summary for entry in result],
            ["방송 종료를 부정함", "게임 종료"],
        )

    def test_final_cleanup_cannot_drop_a_verified_broadcast_signoff(self):
        segments = [
            TranscriptSegment("topic", 100, 110, "게임 이야기를 합니다."),
            TranscriptSegment(
                "ending",
                500,
                510,
                "이제 방송 끌게요. 다들 잘 자요.",
            ),
        ]
        final_entries = [TimelineEntry("topic", 100, "게임 이야기", "게임")]
        chunk_entries = [
            TimelineEntry("ending", 500, "방송 마무리", "방송 종료")
        ]

        result = preserve_broadcast_ending_quotes(
            final_entries,
            chunk_entries,
            segments,
        )

        self.assertEqual([entry.segment_id for entry in result], ["topic", "ending"])
        self.assertEqual(result[-1].summary, "")
        self.assertEqual(result[-1].quote, "이제 방송 끌게요.")

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
            f"{AI_TIMELINE_NOTICE}\n\n"
            "오늘의 콘텐츠: 테스트 콘텐츠\n\n"
            "01:02:05 시청자와 꿈 이야기\n",
        )

    def test_overall_summary_covers_representative_broadcast_topics(self):
        entries = [
            TimelineEntry("s0", 0, "방송 시작", "방송 시작"),
            TimelineEntry("s1", 60, "리롤드 게임의 중독성과 조작 난이도", "리롤드"),
            TimelineEntry("s2", 600, "건월드 게임 특징과 멀티플레이 고민", "건월드"),
            TimelineEntry("s3", 1_200, "다이어트 식단 관리와 운동", "다이어트"),
            TimelineEntry("s4", 1_800, "월드컵 결승전 시청 계획", "월드컵"),
            TimelineEntry("s5", 2_000, "방송 종료", "방송 종료"),
        ]

        result = build_overall_summary(sample_vod(), [], entries)

        self.assertIn("리롤드", result)
        self.assertIn("건월드", result)
        self.assertIn("다이어트", result)
        self.assertIn("월드컵", result)
        self.assertNotIn("방송 시작", result)
        self.assertNotIn("방송 종료", result)

    def test_copied_video_or_single_topic_title_is_replaced_locally(self):
        entries = [
            TimelineEntry("s1", 10, "꿈 이야기", "꿈"),
            TimelineEntry("s2", 300, "게임 시작", "게임"),
        ]

        result = resolve_overall_summary(
            sample_vod().title,
            sample_vod(),
            ["꿈 토크", "게임 플레이"],
            entries,
        )

        self.assertEqual(result, "꿈 이야기 · 게임 시작")
        self.assertNotEqual(result, sample_vod().title)
        self.assertEqual(
            resolve_overall_summary(
                "꿈 이야기",
                sample_vod(),
                ["꿈 토크", "게임 플레이"],
                entries,
            ),
            "꿈 이야기 · 게임 시작",
        )

    def test_gemini_prompts_require_concise_title_style(self):
        chunk_prompt = build_chunk_prompt(sample_vod(), sample_transcript().segments)
        final_prompt = build_final_prompt(
            sample_vod(),
            ["게임 합방"],
            [TimelineEntry("s000000", 0, "방송을 시작합니다.")],
            sample_transcript().segments,
        )
        overall_prompt = build_overall_summary_prompt(
            sample_vod(),
            [
                TimelineEntry("s000000", 0, "방송 시작"),
                TimelineEntry("s000001", 2_600, "꿈 이야기"),
                TimelineEntry("s000002", 2_800, "게임 시작"),
            ],
        )

        for prompt in (chunk_prompt, final_prompt):
            self.assertIn("타임라인 소제목", prompt)
            self.assertIn("존댓말 종결어미와 마침표를 사용하지 않습니다", prompt)
            self.assertIn("여름·겨울 여행 환경과 선호 비교", prompt)
            self.assertIn("현재 방송", prompt)
            self.assertIn("summary는 비웁니다", prompt)
        self.assertIn("문체 규칙에 맞게 간결하고 자연스럽게", final_prompt)
        self.assertIn("타임라인 entries만 정리", final_prompt)
        self.assertIn("별도 단계에서 딱 한 번만", final_prompt)
        self.assertNotIn("전체 방송 한 줄 요약", final_prompt)
        self.assertNotIn("시작·중간·후반의 대표 흐름", final_prompt)
        self.assertIn("전체 방송 요약이 아니라", chunk_prompt)
        self.assertIn("영상 제목을 복사하지 않습니다", chunk_prompt)
        self.assertIn("유일한 작업", overall_prompt)
        self.assertIn("시작·중간·후반의 대표 흐름", overall_prompt)
        self.assertIn("content_title 하나만", overall_prompt)

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
                "entries": [
                    {"segment_id": "s000000", "summary": "방송을 시작함"},
                    {"segment_id": "s000001", "summary": "꿈 이야기를 함"},
                    {"segment_id": "s000002", "summary": "게임을 시작함"},
                ],
            },
            {"content_title": "테스트 콘텐츠"},
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

    def test_generator_replaces_copied_overall_title_without_retrying(self):
        from soop_timeline.services.gemini_timeline import GeminiTimelineGenerator

        payloads = [
            {
                "content_title": sample_vod().title,
                "entries": [
                    {"segment_id": "s000000", "summary": "방송을 시작함"},
                ],
            },
            {
                "content_title": sample_vod().title,
                "entries": [
                    {"segment_id": "s000001", "summary": "꿈 이야기"},
                    {"segment_id": "s000002", "summary": "게임 시작"},
                ],
            },
            {
                "entries": [
                    {"segment_id": "s000000", "summary": "방송을 시작함"},
                    {"segment_id": "s000001", "summary": "꿈 이야기"},
                    {"segment_id": "s000002", "summary": "게임 시작"},
                ],
            },
            {"content_title": sample_vod().title},
        ]
        generator = GeminiTimelineGenerator("test-key")

        with patch.object(generator, "_request_json", side_effect=payloads) as request:
            result = generator.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
            )

        self.assertEqual(request.call_count, 4)
        self.assertEqual(result.content_title, "꿈 이야기 · 게임 시작")
        self.assertNotEqual(result.content_title, sample_vod().title)

    def test_final_timeline_and_overall_title_use_separate_requests(self):
        from soop_timeline.services.gemini_timeline import GeminiTimelineGenerator

        payloads = [
            {
                "content_title": "방송 시작",
                "entries": [
                    {"segment_id": "s000000", "summary": "방송 시작"},
                ],
            },
            {
                "content_title": "꿈과 게임",
                "entries": [
                    {"segment_id": "s000001", "summary": "꿈 이야기"},
                    {"segment_id": "s000002", "summary": "게임 시작"},
                ],
            },
            {
                "entries": [
                    {"segment_id": "s000000", "summary": "방송 시작"},
                    {"segment_id": "s000001", "summary": "꿈 이야기"},
                    {"segment_id": "s000002", "summary": "게임 시작"},
                ],
            },
            {"content_title": "꿈 이야기와 게임 시작"},
        ]
        calls: list[tuple[str, dict[str, object], str]] = []

        def request(prompt, _cancelled, *, schema=None, purpose="timeline"):
            calls.append((prompt, schema, purpose))
            return payloads[len(calls) - 1]

        generator = GeminiTimelineGenerator("test-key")
        with patch.object(generator, "_request_json", side_effect=request):
            result = generator.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
            )

        self.assertEqual(len(calls), 4)
        self.assertIs(calls[-2][1], FINAL_TIMELINE_SCHEMA)
        self.assertEqual(calls[-2][2], "timeline_finalize")
        self.assertEqual(set(FINAL_TIMELINE_SCHEMA["properties"]), {"entries"})
        self.assertIs(calls[-1][1], OVERALL_SUMMARY_SCHEMA)
        self.assertEqual(calls[-1][2], "overall_summary")
        self.assertEqual(
            set(OVERALL_SUMMARY_SCHEMA["properties"]),
            {"content_title"},
        )
        self.assertEqual(result.content_title, "꿈 이야기와 게임 시작")
        self.assertEqual(len(result.entries), 3)

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
        with patch.object(
            resumed,
            "_request_json",
            side_effect=[final_payload, {"content_title": "최종 콘텐츠"}],
        ) as request:
            result = resumed.generate(
                sample_vod(),
                sample_transcript(),
                progress=lambda *args: None,
                cancelled=lambda: False,
                checkpoint_key="checkpoint-key",
                resume_state=checkpoints[-1],
            )
        self.assertEqual(request.call_count, 2)
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
        self.assertTrue(result.startswith(f"{AI_TIMELINE_NOTICE}\n\n"))
        self.assertEqual(result.splitlines()[3], "오늘의 콘텐츠: 최종 라이브")
        self.assertTrue(any(stage == "live_timeline" for stage, _ in previews))

    def test_live_gemini_wait_does_not_block_new_whisper_updates(self):
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
            stream_url="https://example.test/live.m3u8",
        )
        first = TranscriptSegment("s000000", 3_600, 3_665, "첫 주제")
        second = TranscriptSegment("s000001", 3_670, 3_730, "둘째 주제")
        generator_started = threading.Event()
        second_update_sent = threading.Event()

        class FakeTranscriber:
            def transcribe_live(self, source, update, **kwargs):
                del source, kwargs
                update(
                    LiveTranscriptUpdate(
                        "large-v3-turbo",
                        "ko",
                        first.end,
                        (first,),
                    )
                )
                if not generator_started.wait(2.0):
                    raise RuntimeError("Gemini worker did not start")
                update(
                    LiveTranscriptUpdate(
                        "large-v3-turbo",
                        "ko",
                        second.end,
                        (second,),
                    )
                )
                second_update_sent.set()
                return Transcript(
                    "large-v3-turbo",
                    "ko",
                    second.end,
                    [first, second],
                )

        class FakeGenerator:
            def summarize_live_window(
                self,
                vod,
                segments,
                cancelled,
                previous_entries=None,
            ):
                del vod, cancelled, previous_entries
                generator_started.set()
                if not second_update_sent.wait(2.0):
                    raise RuntimeError("Whisper was blocked by Gemini")
                return GeneratedTimeline(
                    "라이브",
                    [TimelineEntry(segments[0].segment_id, segments[0].start, "첫 주제")],
                )

            def finalize_live_entries(self, vod, titles, entries, segments, cancelled):
                del vod, titles, segments, cancelled
                return GeneratedTimeline("최종 라이브", entries)

        analyzer = LocalWhisperGeminiAnalyzer(
            AnalyzerConfig(gemini_api_key="test", live_ai_mode="frequent"),
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
            )

        self.assertTrue(second_update_sent.is_set())
        self.assertIn("첫 주제", result)

    def test_fast_live_shutdown_saves_transcript_without_new_gemini_calls(self):
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
            stream_url="https://example.test/live.m3u8",
        )
        segment = TranscriptSegment("s000000", 3_600, 3_610, "저장할 자막")
        transcript = Transcript(
            "large-v3-turbo",
            "ko",
            segment.end,
            [segment],
        )

        class FakeTranscriber:
            def transcribe_live(self, source, update, **kwargs):
                del source, kwargs
                update(
                    LiveTranscriptUpdate(
                        transcript.model,
                        transcript.language,
                        transcript.duration_seconds,
                        (segment,),
                    )
                )
                return transcript

        class NoCallGenerator:
            def summarize_live_window(self, *args, **kwargs):
                raise AssertionError("fast shutdown must not summarize")

            def finalize_live_entries(self, *args, **kwargs):
                raise AssertionError("fast shutdown must not finalize")

        analyzer = LocalWhisperGeminiAnalyzer(
            AnalyzerConfig(gemini_api_key="test"),
            transcriber_factory=lambda model, device: FakeTranscriber(),
            generator_factory=lambda key, model: NoCallGenerator(),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "soop_timeline.services.analyzer.analysis_data_dir",
            return_value=Path(directory),
        ):
            with self.assertRaises(AnalysisCancelled):
                analyzer.analyze_live(
                    live_vod,
                    source,
                    progress=lambda *args: None,
                    stop_requested=lambda: True,
                    finalize_requested=lambda: False,
                )
            recovered = load_cached_transcript(live_vod)
            self.assertEqual([item.text for item in recovered.segments], ["저장할 자막"])
            self.assertFalse(
                (Path(directory) / LIVE_TRANSCRIPT_JOURNAL_FILENAME).exists()
            )

    def test_live_transcript_journal_appends_only_new_items_and_recovers(self):
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
            stream_url="https://example.test/live.m3u8",
        )
        first = Transcript(
            "large-v3-turbo",
            "ko",
            3_615,
            [TranscriptSegment("s000000", 3_600, 3_610, "첫 구간")],
            (TranscriptWord(3_600, 3_601, "첫"),),
        )
        second = Transcript(
            "large-v3-turbo",
            "ko",
            3_630,
            [
                *first.segments,
                TranscriptSegment("s000001", 3_620, 3_630, "둘째 구간"),
            ],
            (*first.words, TranscriptWord(3_620, 3_621, "둘째")),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "soop_timeline.services.analyzer.analysis_data_dir",
            return_value=Path(directory),
        ):
            journal = _LiveTranscriptJournal(live_vod, source)
            journal.append_update(
                LiveTranscriptUpdate(
                    first.model,
                    first.language,
                    first.duration_seconds,
                    tuple(first.segments),
                    first.words,
                )
            )
            journal.append_update(
                LiveTranscriptUpdate(
                    second.model,
                    second.language,
                    second.duration_seconds,
                    (second.segments[-1],),
                    (second.words[-1],),
                )
            )
            lines = (Path(directory) / LIVE_TRANSCRIPT_JOURNAL_FILENAME).read_text(
                encoding="utf-8"
            ).splitlines()
            self.assertEqual(len(lines), 3)
            second_append = json.loads(lines[2])
            self.assertEqual(
                [item["segment_id"] for item in second_append["segments"]],
                ["s000001"],
            )
            self.assertEqual(
                [item["text"] for item in second_append["words"]],
                ["둘째"],
            )
            recovered = load_cached_transcript(live_vod)
            self.assertEqual([item.text for item in recovered.segments], ["첫 구간", "둘째 구간"])

            journal.finalize(second)
            self.assertTrue((Path(directory) / LIVE_TRANSCRIPT_FILENAME).is_file())
            self.assertFalse(
                (Path(directory) / LIVE_TRANSCRIPT_JOURNAL_FILENAME).exists()
            )
            self.assertEqual(len(load_cached_transcript(live_vod).segments), 2)

    def test_eight_hour_live_journal_keeps_each_record_incremental(self):
        live_vod = sample_vod()
        live_vod.source_kind = "live"
        live_vod.url = "https://play.sooplive.com/sample/98765"
        source = LiveAudioSource(
            kind="live",
            channel_id="sample",
            streamer_name="샘플",
            broadcast_no="98765",
            title="장시간 라이브 테스트",
            page_url=live_vod.url,
            runtime_seconds=0,
            stream_url="https://example.test/live.m3u8",
        )
        chunk_count = (8 * 60 * 60) // 15

        with tempfile.TemporaryDirectory() as directory, patch(
            "soop_timeline.services.analyzer.analysis_data_dir",
            return_value=Path(directory),
        ):
            journal = _LiveTranscriptJournal(live_vod, source)
            for index in range(chunk_count):
                start = float(index * 15)
                journal.append_update(
                    LiveTranscriptUpdate(
                        "large-v3-turbo",
                        "ko",
                        start + 15,
                        (
                            TranscriptSegment(
                                f"s{index:06d}",
                                start,
                                start + 5,
                                f"구간 {index}",
                            ),
                        ),
                    )
                )

            path = Path(directory) / LIVE_TRANSCRIPT_JOURNAL_FILENAME
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), chunk_count + 1)
            for line_number in (1, chunk_count // 2, chunk_count):
                record = json.loads(lines[line_number])
                self.assertEqual(len(record["segments"]), 1)
            recovered = load_cached_transcript(live_vod)
            self.assertIsNotNone(recovered)
            self.assertEqual(len(recovered.segments), chunk_count)
            self.assertEqual(recovered.segments[-1].segment_id, "s001919")


if __name__ == "__main__":
    unittest.main()
