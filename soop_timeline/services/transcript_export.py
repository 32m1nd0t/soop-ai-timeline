from __future__ import annotations

from .transcription import Transcript, format_timestamp, transcript_preview_document


def transcript_to_text(transcript: Transcript) -> str:
    return transcript_preview_document(transcript.segments)


def transcript_to_srt(transcript: Transcript) -> str:
    blocks: list[str] = []
    for segment in transcript.segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        index = len(blocks) + 1
        blocks.append(
            f"{index}\n{_srt_time(segment.start)} --> {_srt_time(segment.end)}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def transcript_summary(transcript: Transcript) -> str:
    return (
        f"Whisper {transcript.model} · {len(transcript.segments):,}개 자막 · "
        f"마지막 시각 {format_timestamp(transcript.duration_seconds)}"
    )


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(float(seconds) * 1_000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
