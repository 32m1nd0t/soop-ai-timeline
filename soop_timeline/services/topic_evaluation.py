from __future__ import annotations

from dataclasses import dataclass

from .gemini_timeline import TimelineEntry


@dataclass(frozen=True, slots=True)
class TopicBoundaryScore:
    expected: int
    predicted: int
    matched: int
    precision: float
    recall: float
    f1: float


def evaluate_topic_boundaries(
    predicted: list[TimelineEntry],
    expected_seconds: list[float],
    *,
    tolerance_seconds: float = 30.0,
) -> TopicBoundaryScore:
    remaining = sorted(max(0.0, float(value)) for value in expected_seconds)
    matched = 0
    for entry in sorted(predicted, key=lambda item: item.start):
        nearest_index = _nearest_within(
            remaining,
            max(0.0, float(entry.start)),
            tolerance_seconds,
        )
        if nearest_index is not None:
            matched += 1
            remaining.pop(nearest_index)
    predicted_count = len(predicted)
    expected_count = len(expected_seconds)
    precision = matched / predicted_count if predicted_count else 0.0
    recall = matched / expected_count if expected_count else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return TopicBoundaryScore(
        expected_count,
        predicted_count,
        matched,
        precision,
        recall,
        f1,
    )


def _nearest_within(
    values: list[float],
    target: float,
    tolerance: float,
) -> int | None:
    candidates = [
        (abs(value - target), index)
        for index, value in enumerate(values)
        if abs(value - target) <= tolerance
    ]
    return min(candidates)[1] if candidates else None
