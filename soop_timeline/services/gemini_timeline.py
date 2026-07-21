from __future__ import annotations

import json
import time
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from ..models import Vod
from .eta import EtaEstimator, format_eta
from .gemini_style import DRY_TIMELINE_STYLE_GUIDE
from .transcription import (
    AnalysisCancelled,
    CancelCallback,
    PreviewCallback,
    ProgressCallback,
    Transcript,
    TranscriptSegment,
    format_timestamp,
)


DEFAULT_TOPIC_GRANULARITY = "broad"
TOPIC_GRANULARITIES = {"broad", "balanced", "detailed"}


@dataclass(slots=True, frozen=True)
class TimelineEntry:
    segment_id: str
    start: float
    summary: str


@dataclass(slots=True)
class GeneratedTimeline:
    content_title: str
    entries: list[TimelineEntry]

    def to_document(self) -> str:
        lines = [f"오늘의 콘텐츠: {self.content_title.strip()}", ""]
        lines.extend(
            f"{format_timestamp(entry.start)} {entry.summary.strip()}"
            for entry in self.entries
            if entry.summary.strip()
        )
        return "\n".join(lines).rstrip() + "\n"


TIMELINE_SCHEMA = {
    "type": "object",
    "properties": {
        "content_title": {
            "type": "string",
            "description": "존댓말 종결어미가 없는 짧은 타임라인 제목",
        },
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "string"},
                    "summary": {
                        "type": "string",
                        "description": "합니다/입니다체가 아닌 간결한 제목형·메모체 요약",
                    },
                },
                "required": ["segment_id", "summary"],
            },
        },
    },
    "required": ["content_title", "entries"],
}


class GeminiTimelineGenerator:
    def __init__(
        self,
        api_key: str,
        model_name: str = "gemini-3.5-flash",
        topic_granularity: str = DEFAULT_TOPIC_GRANULARITY,
    ):
        self.api_key = api_key.strip()
        self.model_name = model_name.strip() or "gemini-3.5-flash"
        self.topic_granularity = normalize_topic_granularity(topic_granularity)

    def generate(
        self,
        vod: Vod,
        transcript: Transcript,
        progress: ProgressCallback,
        cancelled: CancelCallback,
        preview: PreviewCallback | None = None,
    ) -> GeneratedTimeline:
        if not self.api_key:
            raise RuntimeError("Gemini API 키를 먼저 설정하세요.")
        try:
            from google import genai
            from google.genai import types
        except ImportError as error:
            raise RuntimeError(
                "Gemini API 모듈(google-genai)이 설치되지 않았습니다."
            ) from error

        client = genai.Client(api_key=self.api_key)
        windows = split_transcript(transcript.segments)
        candidates: list[TimelineEntry] = []
        titles: list[str] = []
        segment_lookup = {segment.segment_id: segment for segment in transcript.segments}
        eta = EtaEstimator(len(windows) + 1)

        for index, window in enumerate(windows):
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            start_label = format_timestamp(window[0].start)
            end_label = format_timestamp(window[-1].end)
            percent = 70 + int((index / max(1, len(windows))) * 20)
            progress(
                percent,
                f"Gemini가 {start_label}~{end_label} 구간의 대화 주제를 정리합니다… · "
                f"{format_eta(eta.remaining_seconds(index))}",
            )
            prompt = build_chunk_prompt(
                vod,
                window,
                previous_entries=deduplicate_entries(candidates)[-8:],
                granularity=self.topic_granularity,
            )
            payload = self._request_json(client, types, prompt, cancelled)
            title = str(payload.get("content_title", "")).strip()
            if title:
                titles.append(title)
            candidates.extend(entries_from_payload(payload, segment_lookup))

            completed = index + 1
            draft_entries = deduplicate_entries(candidates)
            if preview is not None and draft_entries:
                preview(
                    "timeline",
                    GeneratedTimeline(
                        content_title=titles[0] if titles else vod.title,
                        entries=draft_entries,
                    ).to_document(),
                )
            progress(
                70 + int((completed / max(1, len(windows))) * 20),
                f"Gemini 임시 타임라인 {completed}/{len(windows)} 구간 완료 · "
                f"{format_eta(eta.remaining_seconds(completed))}",
            )

        candidates = deduplicate_entries(candidates)
        if not candidates:
            raise RuntimeError("Gemini가 유효한 타임라인 항목을 만들지 못했습니다.")

        if cancelled():
            raise AnalysisCancelled("분석을 취소했습니다.")
        progress(
            92,
            "전체 구간의 중복을 정리하고 최종 타임라인을 구성합니다… · "
            f"{format_eta(eta.remaining_seconds(len(windows)))}",
        )
        final_prompt = build_final_prompt(
            vod,
            titles,
            candidates,
            transcript.segments,
            granularity=self.topic_granularity,
        )
        final_payload = self._request_json(client, types, final_prompt, cancelled)
        final_entries = entries_from_payload(final_payload, segment_lookup)
        final_entries = deduplicate_entries(final_entries) or candidates
        content_title = str(final_payload.get("content_title", "")).strip()
        if not content_title:
            content_title = titles[0] if titles else vod.title

        if preview is not None:
            preview(
                "timeline",
                GeneratedTimeline(
                    content_title=content_title,
                    entries=final_entries,
                ).to_document(),
            )
        progress(99, f"타임라인 {len(final_entries):,}개 항목을 만들었습니다.")
        return GeneratedTimeline(content_title=content_title, entries=final_entries)

    def summarize_live_window(
        self,
        vod: Vod,
        segments: list[TranscriptSegment],
        cancelled: CancelCallback,
        previous_entries: list[TimelineEntry] | None = None,
    ) -> GeneratedTimeline:
        if not self.api_key:
            raise RuntimeError("Gemini API 키를 먼저 설정하세요.")
        if not segments:
            return GeneratedTimeline(vod.title, [])
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        payload = self._request_json(
            client,
            types,
            build_chunk_prompt(
                vod,
                segments,
                previous_entries=previous_entries,
                granularity=self.topic_granularity,
            ),
            cancelled,
        )
        lookup = {segment.segment_id: segment for segment in segments}
        title = str(payload.get("content_title", "") or "").strip() or vod.title
        return GeneratedTimeline(
            content_title=title,
            entries=deduplicate_entries(entries_from_payload(payload, lookup)),
        )

    def finalize_live_entries(
        self,
        vod: Vod,
        titles: list[str],
        entries: list[TimelineEntry],
        segments: list[TranscriptSegment],
        cancelled: CancelCallback,
    ) -> GeneratedTimeline:
        candidates = deduplicate_entries(entries)
        if not candidates:
            raise RuntimeError("Gemini가 유효한 라이브 타임라인 항목을 만들지 못했습니다.")
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        payload = self._request_json(
            client,
            types,
            build_final_prompt(
                vod,
                titles,
                candidates,
                segments,
                granularity=self.topic_granularity,
            ),
            cancelled,
        )
        lookup = {segment.segment_id: segment for segment in segments}
        final_entries = deduplicate_entries(entries_from_payload(payload, lookup))
        title = str(payload.get("content_title", "") or "").strip()
        return GeneratedTimeline(
            content_title=title or (titles[0] if titles else vod.title),
            entries=final_entries or candidates,
        )

    def _request_json(self, client: object, types: object, prompt: str, cancelled: CancelCallback) -> dict[str, object]:
        last_error: Exception | None = None
        for attempt in range(3):
            if cancelled():
                raise AnalysisCancelled("분석을 취소했습니다.")
            try:
                config = types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                    response_schema=TIMELINE_SCHEMA,
                )
                response = client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=config,
                )
                text = str(getattr(response, "text", "") or "").strip()
                if not text:
                    raise RuntimeError("Gemini가 빈 응답을 반환했습니다.")
                payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise RuntimeError("Gemini 응답 형식이 올바르지 않습니다.")
                return payload
            except AnalysisCancelled:
                raise
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 ** attempt)
        raise RuntimeError(f"Gemini 요청에 실패했습니다: {last_error}") from last_error


def split_transcript(
    segments: list[TranscriptSegment],
    window_seconds: float = 45 * 60,
    overlap_seconds: float = 2 * 60,
) -> list[list[TranscriptSegment]]:
    if not segments:
        return []
    windows: list[list[TranscriptSegment]] = []
    start_index = 0
    while start_index < len(segments):
        limit = segments[start_index].start + window_seconds
        end_index = start_index + 1
        while end_index < len(segments) and segments[end_index].start < limit:
            end_index += 1
        windows.append(segments[start_index:end_index])
        if end_index >= len(segments):
            break
        next_start_time = max(segments[start_index].start, segments[end_index - 1].end - overlap_seconds)
        next_index = start_index + 1
        while next_index < end_index and segments[next_index].start < next_start_time:
            next_index += 1
        start_index = next_index if next_index < end_index else end_index
    return windows


def build_chunk_prompt(
    vod: Vod,
    segments: Iterable[TranscriptSegment],
    previous_entries: list[TimelineEntry] | None = None,
    granularity: str = DEFAULT_TOPIC_GRANULARITY,
) -> str:
    segment_list = list(segments)
    transcript_text = "\n".join(
        f"{segment.segment_id} | {format_timestamp(segment.start)} | {segment.text}"
        for segment in segment_list
    )
    prior = deduplicate_entries(previous_entries or [])[-8:]
    previous_topic_text = "\n".join(
        f"- {format_timestamp(entry.start)} {entry.summary}" for entry in prior
    ) or "- 없음"
    media_label = "라이브 방송" if vod.source_kind == "live" else "다시보기"
    return f"""
당신은 한국 인터넷 방송 {media_label}의 주제 경계 기반 타임라인 편집자입니다.

영상 제목: {vod.title}
스트리머: {vod.streamer_name}

이 작업은 일정 시간마다 자막을 요약하는 작업이 아닙니다. 아래 자막을 시간순으로 읽고,
하나의 중심 주제로 이어지는 대화는 길이에 관계없이 한 묶음으로 유지한 뒤 실제로 새 주제가
시작되는 첫 발화만 타임라인 항목으로 고르세요.

주제 묶음 수준:
{topic_granularity_guide(granularity)}

직전까지 확인된 주제:
{previous_topic_text}

경계 판정 규칙:
- 자막 첫 부분이 직전 주제의 계속이라면 새 항목을 만들지 않습니다.
- 같은 중심 질문이나 활동에 속한 배경 설명·과거 경험·구체적 사례·이유·장단점·추가 반응은 하나의 주제로 묶습니다.
- 단순히 시간이 지났거나 화자가 잠깐 말을 멈췄다는 이유로 항목을 추가하지 않습니다.
- 대화의 중심 질문·사건·게임 활동·공지 대상이 실제로 달라질 때만 새 주제로 판정합니다.
- 잡담 중 짧게 언급하고 바로 원래 이야기로 돌아온 내용은 독립 항목으로 만들지 않습니다.
- 서로 무관한 대화가 사이에 이어진 뒤 예전 주제로 돌아온 경우에는 `복귀` 성격의 새 항목을 만들 수 있습니다.
- 주제 시작이 확실하지 않으면 성급히 쪼개지 말고 앞 주제에 포함합니다.

출력 규칙:
- segment_id는 반드시 아래 `이번 자막`에 실제로 존재하는 값만 사용합니다. 직전 주제의 ID는 반환하지 않습니다.
- 각 항목에는 그 주제가 처음 시작되는 발화의 segment_id를 사용합니다.
- 시간은 만들지 않습니다. 프로그램이 segment_id의 원래 시간을 사용합니다.
- 요약은 댓글에 바로 붙일 수 있는 구체적인 한국어 한 줄로 작성합니다.
- 스트리머가 말하지 않은 사실을 추측하지 않습니다.
- 광고, 장시간 무음, 단순 배경음은 제외합니다.
- 목표 항목 수를 정해 두지 말고 실제 주제 전환 수에 맞춥니다. 새 주제가 없다면 entries를 빈 배열로 반환합니다.

{DRY_TIMELINE_STYLE_GUIDE}

이번 자막:
{transcript_text}
""".strip()


def build_final_prompt(
    vod: Vod,
    titles: list[str],
    entries: list[TimelineEntry],
    segments: list[TranscriptSegment] | None = None,
    granularity: str = DEFAULT_TOPIC_GRANULARITY,
) -> str:
    candidate_text = "\n".join(
        f"{entry.segment_id} | {format_timestamp(entry.start)} | {entry.summary}"
        for entry in entries
    )
    evidence_text = build_candidate_evidence(entries, segments or [])
    title_text = " / ".join(titles[:20])
    return f"""
아래는 긴 인터넷 방송을 여러 구간으로 나누어 만든 타임라인 후보입니다.

영상 제목: {vod.title}
구간별 콘텐츠 제목 후보: {title_text}

후보를 한 줄씩 단순히 고쳐 쓰지 말고, 전체 방송의 주제 흐름을 다시 판정해 최종 타임라인을 만드세요.

주제 묶음 수준:
{topic_granularity_guide(granularity)}

규칙:
- segment_id는 후보에 실제로 존재하는 값만 그대로 사용합니다.
- 같은 중심 질문이나 활동이 연속되는 동안 나온 배경·추억·사례·이유·장단점·반응 후보는 한 주제로 병합합니다.
- 병합할 때는 해당 주제가 처음 시작된 가장 이른 후보의 segment_id를 사용합니다.
- 시간 간격이나 후보 문장의 단어 차이만으로 새 주제라고 판단하지 않습니다.
- 중심 사건·질문·게임 활동·공지 대상이 실제로 달라진 후보는 유지합니다.
- 잠깐 다른 말을 한 뒤 즉시 원래 주제로 돌아온 경우 그 짧은 언급은 별도 항목으로 만들지 않습니다.
- 서로 무관한 주제가 충분히 이어진 뒤 과거 주제로 복귀한 경우에는 시간 흐름을 위해 별도 항목으로 유지할 수 있습니다.
- 시간순으로 정렬합니다.
- content_title은 방송 전체의 핵심 콘텐츠를 짧은 제목형으로 작성합니다.
- 각 summary의 정보는 유지하되 아래 문체 규칙에 맞게 반드시 다시 작성합니다.

{DRY_TIMELINE_STYLE_GUIDE}

후보:
{candidate_text}

후보 주변 원문 근거(판정 참고용이며, 이 구역의 segment_id는 새로 선택하지 않음):
{evidence_text or '- 없음'}
""".strip()


def normalize_topic_granularity(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in TOPIC_GRANULARITIES else DEFAULT_TOPIC_GRANULARITY


def topic_granularity_guide(value: str) -> str:
    granularity = normalize_topic_granularity(value)
    if granularity == "detailed":
        return (
            "촘촘하게: 중심 주제는 유지하되 독립적으로 검수할 가치가 있는 질문·사건·게임 국면은 "
            "세부 항목으로 분리합니다. 단순 반복과 부연 설명은 합칩니다."
        )
    if granularity == "balanced":
        return (
            "기본: 같은 대화의 부연 설명과 사례는 묶고, 시청자가 별도 지점으로 찾아갈 만한 "
            "명확한 소주제 전환만 분리합니다."
        )
    return (
        "큰 주제 위주: 하나의 중심 토크나 활동 아래 이어지는 세부 질문·추억·사례·이유·장단점은 "
        "가급적 첫 시작점 한 항목으로 묶습니다. 완전히 다른 중심 주제로 넘어갈 때만 분리합니다."
    )


def build_candidate_evidence(
    entries: list[TimelineEntry],
    segments: list[TranscriptSegment],
    radius_before: int = 1,
    radius_after: int = 2,
) -> str:
    if not entries or not segments:
        return ""
    index_by_id = {
        segment.segment_id: index for index, segment in enumerate(segments)
    }
    selected_indexes: set[int] = set()
    for entry in entries:
        index = index_by_id.get(entry.segment_id)
        if index is None:
            continue
        start = max(0, index - radius_before)
        end = min(len(segments), index + radius_after + 1)
        selected_indexes.update(range(start, end))
    return "\n".join(
        f"{segments[index].segment_id} | "
        f"{format_timestamp(segments[index].start)} | {segments[index].text}"
        for index in sorted(selected_indexes)
    )


def entries_from_payload(
    payload: dict[str, object],
    segment_lookup: dict[str, TranscriptSegment],
) -> list[TimelineEntry]:
    result: list[TimelineEntry] = []
    raw_entries = payload.get("entries", [])
    if not isinstance(raw_entries, list):
        return result
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        segment_id = str(raw.get("segment_id", "")).strip()
        summary = " ".join(str(raw.get("summary", "")).split())
        segment = segment_lookup.get(segment_id)
        if segment is None or not summary:
            continue
        result.append(
            TimelineEntry(
                segment_id=segment_id,
                start=segment.start,
                summary=summary,
            )
        )
    return result


def deduplicate_entries(entries: list[TimelineEntry]) -> list[TimelineEntry]:
    ordered = sorted(entries, key=lambda item: (item.start, item.segment_id))
    result: list[TimelineEntry] = []
    seen_ids: set[str] = set()
    for entry in ordered:
        if entry.segment_id in seen_ids:
            continue
        if result and entry.start - result[-1].start <= 180:
            similarity = SequenceMatcher(
                None,
                normalize_summary(result[-1].summary),
                normalize_summary(entry.summary),
            ).ratio()
            if similarity >= 0.78:
                continue
        result.append(entry)
        seen_ids.add(entry.segment_id)
    return result


def normalize_summary(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())
