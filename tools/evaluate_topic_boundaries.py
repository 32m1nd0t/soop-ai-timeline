from __future__ import annotations

import json
from pathlib import Path
import sys

from soop_timeline.services.gemini_timeline import TimelineEntry
from soop_timeline.services.topic_evaluation import evaluate_topic_boundaries


def main(arguments: list[str]) -> int:
    if len(arguments) not in {1, 3}:
        print(
            "사용법: python tools/evaluate_topic_boundaries.py "
            "<cases.json> <predictions.json>"
        )
        return 2
    if len(arguments) == 1:
        root = Path(__file__).resolve().parents[1]
        cases_path = root / "evaluation" / "topic_boundary_cases.json"
        predictions_path = root / "evaluation" / "example_predictions.json"
    else:
        cases_path = Path(arguments[1])
        predictions_path = Path(arguments[2])

    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    predictions = json.loads(predictions_path.read_text(encoding="utf-8"))
    total_matched = total_expected = total_predicted = 0
    for case in cases:
        predicted_seconds = predictions.get(case["id"], [])
        entries = [
            TimelineEntry(f"p{index}", float(seconds), "평가 예측")
            for index, seconds in enumerate(predicted_seconds)
        ]
        score = evaluate_topic_boundaries(entries, case["expected_seconds"])
        total_matched += score.matched
        total_expected += score.expected
        total_predicted += score.predicted
        print(
            f"{case['id']}: precision={score.precision:.3f} "
            f"recall={score.recall:.3f} f1={score.f1:.3f}"
        )

    precision = total_matched / total_predicted if total_predicted else 0.0
    recall = total_matched / total_expected if total_expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    print(f"TOTAL: precision={precision:.3f} recall={recall:.3f} f1={f1:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
