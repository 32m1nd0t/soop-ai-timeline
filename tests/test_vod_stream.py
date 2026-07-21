import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from soop_timeline.models import Vod
from soop_timeline.services.transcription import (
    FasterWhisperTranscriber,
    Transcript,
    TranscriptSegment,
    WhisperRuntime,
    _WhisperBackend,
    load_vod_transcript_cache,
    save_vod_transcript_cache,
)
from soop_timeline.services.live_stream import LiveAudioSource
from soop_timeline.services.vod_stream import (
    AudioChunk,
    VOD_INFO_URL,
    VodAudioPart,
    VodAudioSource,
    fetch_vod_audio_source,
)


def sample_vod() -> Vod:
    return Vod(
        vod_id="123",
        streamer_id=1,
        channel_id="sample",
        streamer_name="샘플",
        title="테스트 방송",
        url="https://vod.sooplive.com/player/123",
        duration_text="6:45:00",
        published_text="방금 전",
        thumbnail_url="",
        state="new",
        discovered_at="",
        updated_at="",
    )


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self) -> str:
        return VOD_INFO_URL

    def read(self, size: int) -> bytes:
        return self.raw[:size]


def public_payload() -> dict[str, object]:
    return {
        "result": 1,
        "data": {
            "is_public": 1,
            "is_paid": False,
            "is_ppv": False,
            "adult_status": "pass",
            "total_file_duration": 24_357_984,
            "files": [
                {
                    "file_order": 2,
                    "duration": 6_357_984,
                    "hide": "N",
                    "radio_url": "https://vod-a.sooplive.com/part2/audio.m3u8",
                },
                {
                    "file_order": 1,
                    "duration": 18_000_000,
                    "hide": "N",
                    "radio_url": "https://vod-a.sooplive.com/part1/audio.m3u8",
                },
            ],
        },
    }


class VodStreamTests(unittest.TestCase):
    def test_public_metadata_returns_every_audio_part_in_order(self):
        messages: list[tuple[int, str]] = []
        with patch(
            "soop_timeline.services.vod_stream.urlopen",
            return_value=FakeResponse(public_payload()),
        ) as mocked:
            source = fetch_vod_audio_source(
                sample_vod(),
                lambda percent, message: messages.append((percent, message)),
                lambda: False,
            )

        request = mocked.call_args.args[0]
        self.assertIn(b"nTitleNo=123", request.data)
        self.assertEqual([part.order for part in source.parts], [1, 2])
        self.assertAlmostEqual(source.total_duration_seconds, 24_357.984)
        self.assertIn("영상 데이터는 받지 않습니다", messages[-1][1])

    def test_non_public_vod_is_rejected(self):
        payload = public_payload()
        payload["data"]["is_public"] = 0
        with patch(
            "soop_timeline.services.vod_stream.urlopen",
            return_value=FakeResponse(payload),
        ):
            with self.assertRaisesRegex(RuntimeError, "전체 공개"):
                fetch_vod_audio_source(
                    sample_vod(), lambda *args: None, lambda: False
                )

    def test_audio_chunk_converts_pcm_without_a_file(self):
        pcm = np.array([-32768, 0, 32767], dtype="<i2").tobytes()
        chunk = AudioChunk(part_order=1, start_seconds=10, pcm_s16=pcm)
        audio = chunk.as_float32()
        self.assertAlmostEqual(float(audio[0]), -1.0)
        self.assertAlmostEqual(float(audio[1]), 0.0)
        self.assertGreater(float(audio[2]), 0.99)

    def test_batched_stream_transcription_splits_overlap_once(self):
        chunks = [
            AudioChunk(1, 0, bytes(20 * 16_000 * 2)),
            AudioChunk(1, 15, bytes(10 * 16_000 * 2)),
        ]

        class FakePipeline:
            def __init__(self):
                self.calls = 0

            def transcribe(self, audio, **kwargs):
                self.calls += 1
                self.assert_audio(audio, kwargs)
                if self.calls == 1:
                    raw = [
                        SimpleNamespace(start=1, end=2, text="시작"),
                        SimpleNamespace(start=17, end=19, text="경계 발화"),
                    ]
                else:
                    raw = [
                        SimpleNamespace(start=2, end=4, text="경계 발화"),
                        SimpleNamespace(start=7, end=8, text="마무리"),
                    ]
                return iter(raw), SimpleNamespace(language="ko")

            @staticmethod
            def assert_audio(audio, kwargs):
                assert audio.dtype == np.float32
                assert kwargs["batch_size"] == 8
                assert kwargs["without_timestamps"] is False

        pipeline = FakePipeline()
        backend = _WhisperBackend(
            runtime=WhisperRuntime("cuda", "float16", "테스트 GPU"),
            model=object(),
            batched_pipeline=pipeline,
        )
        source = VodAudioSource(
            "123",
            25,
            (VodAudioPart(1, 25, "https://vod-a.sooplive.com/audio.m3u8"),),
        )
        transcriber = FasterWhisperTranscriber("large-v3-turbo", "cuda")
        messages: list[str] = []
        previews: list[tuple[str, str]] = []

        with patch.object(transcriber, "_prepare_backend", return_value=backend), patch(
            "soop_timeline.services.vod_stream.iter_audio_chunks",
            return_value=iter(chunks),
        ):
            result = transcriber.transcribe_stream(
                source,
                initial_prompt="테스트",
                progress=lambda percent, message: messages.append(message),
                cancelled=lambda: False,
                preview=lambda stage, text: previews.append((stage, text)),
            )

        self.assertEqual([item.text for item in result.segments], ["시작", "경계 발화", "마무리"])
        self.assertEqual([item.start for item in result.segments], [1, 17, 22])
        self.assertEqual(pipeline.calls, 2)
        self.assertEqual(previews[-1][0], "transcript")
        self.assertIn("00:00:22 마무리", previews[-1][1])
        self.assertTrue(
            any("예상 시간" in message or "예상 완료" in message for message in messages)
        )

    def test_vod_transcript_cache_matches_url_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "transcript.json"
            transcript = Transcript(
                model="large-v3-turbo",
                language="ko",
                duration_seconds=10,
                segments=[TranscriptSegment("s0", 1, 2, "안녕하세요")],
            )
            save_vod_transcript_cache(cache_path, "123", "https://vod/123", transcript)

            self.assertIsNotNone(
                load_vod_transcript_cache(
                    cache_path,
                    "123",
                    "https://vod/123",
                    "large-v3-turbo",
                )
            )
            self.assertIsNone(
                load_vod_transcript_cache(
                    cache_path,
                    "123",
                    "https://vod/changed",
                    "large-v3-turbo",
                )
            )

    def test_stream_transcription_resumes_from_checkpoint(self):
        chunks = [
            AudioChunk(1, 85, bytes(20 * 16_000 * 2)),
            AudioChunk(1, 100, bytes(20 * 16_000 * 2)),
        ]

        class FakePipeline:
            def __init__(self):
                self.calls = 0

            def transcribe(self, audio, **kwargs):
                del audio, kwargs
                self.calls += 1
                if self.calls == 1:
                    raw = [
                        SimpleNamespace(start=1, end=2, text="이미 처리됨"),
                        SimpleNamespace(start=16, end=17, text="재개 발화"),
                    ]
                else:
                    raw = [SimpleNamespace(start=10, end=11, text="마지막 발화")]
                return iter(raw), SimpleNamespace(language="ko")

        backend = _WhisperBackend(
            runtime=WhisperRuntime("cuda", "float16", "테스트 GPU"),
            model=object(),
            batched_pipeline=FakePipeline(),
        )
        source = VodAudioSource(
            "123",
            120,
            (VodAudioPart(1, 120, "https://vod-a.sooplive.com/audio.m3u8"),),
        )
        resume = Transcript(
            model="large-v3-turbo",
            language="ko",
            duration_seconds=100,
            segments=[TranscriptSegment("s000000", 90, 100, "기존 발화")],
        )
        checkpoints = []
        transcriber = FasterWhisperTranscriber("large-v3-turbo", "cuda")
        with patch.object(transcriber, "_prepare_backend", return_value=backend), patch(
            "soop_timeline.services.vod_stream.iter_audio_chunks",
            return_value=iter(chunks),
        ) as iterator:
            result = transcriber.transcribe_stream(
                source,
                initial_prompt="테스트",
                progress=lambda *args: None,
                cancelled=lambda: False,
                resume=resume,
                checkpoint=checkpoints.append,
            )

        self.assertEqual(
            [segment.text for segment in result.segments],
            ["기존 발화", "재개 발화", "마지막 발화"],
        )
        self.assertEqual(iterator.call_args.kwargs["start_seconds"], 85)
        self.assertGreaterEqual(len(checkpoints), 1)

    def test_live_transcription_keeps_broadcast_runtime_timestamps(self):
        chunks = [
            AudioChunk(1, 3_600, bytes(15 * 16_000 * 2)),
            AudioChunk(1, 3_613, bytes(15 * 16_000 * 2)),
        ]

        class FakePipeline:
            def __init__(self):
                self.calls = 0

            def transcribe(self, audio, **kwargs):
                del audio, kwargs
                self.calls += 1
                raw = (
                    [SimpleNamespace(start=1, end=2, text="첫 발화")]
                    if self.calls == 1
                    else [SimpleNamespace(start=2, end=3, text="다음 발화")]
                )
                return iter(raw), SimpleNamespace(language="ko")

        pipeline = FakePipeline()
        backend = _WhisperBackend(
            runtime=WhisperRuntime("cuda", "float16", "테스트 GPU"),
            model=object(),
            batched_pipeline=pipeline,
        )
        source = LiveAudioSource(
            kind="live",
            channel_id="sample",
            streamer_name="샘플",
            broadcast_no="98765",
            title="라이브",
            page_url="https://play.sooplive.com/sample/98765",
            runtime_seconds=3_600,
            stream_url="https://live-pcweb-kr-cdn-z02.sooplive.com/live/auth_playlist.m3u8?aid=x",
        )
        updates = []
        transcriber = FasterWhisperTranscriber("large-v3-turbo", "cuda")
        with patch.object(transcriber, "_prepare_backend", return_value=backend), patch(
            "soop_timeline.services.live_stream.iter_live_audio_chunks",
            return_value=iter(chunks),
        ):
            result = transcriber.transcribe_live(
                source,
                initial_prompt="테스트",
                progress=lambda *args: None,
                stop_requested=lambda: False,
                update=lambda transcript: updates.append(transcript),
            )

        self.assertEqual([item.text for item in result.segments], ["첫 발화", "다음 발화"])
        self.assertEqual([item.start for item in result.segments], [3_601, 3_615])
        self.assertGreaterEqual(len(updates), 2)


if __name__ == "__main__":
    unittest.main()
