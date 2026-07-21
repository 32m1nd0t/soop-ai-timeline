import unittest
from datetime import datetime

from soop_timeline.services.eta import EtaEstimator, format_eta, humanize_duration


class EtaTests(unittest.TestCase):
    def test_estimator_uses_measured_throughput(self):
        current = [100.0]
        estimator = EtaEstimator(100, clock=lambda: current[0])
        current[0] += 10.0

        self.assertAlmostEqual(estimator.remaining_seconds(25) or 0, 30.0)

    def test_estimator_waits_for_a_real_sample(self):
        estimator = EtaEstimator(100, clock=lambda: 1.0)
        self.assertIsNone(estimator.remaining_seconds(10))

    def test_duration_and_completion_labels(self):
        self.assertEqual(humanize_duration(30), "1분 이내")
        self.assertEqual(humanize_duration(61), "2분")
        self.assertEqual(humanize_duration(3_660), "1시간 1분")
        self.assertEqual(
            format_eta(1_200, datetime(2026, 7, 21, 23, 50)),
            "남은 시간 약 20분 · 예상 완료 내일 00:10",
        )
        self.assertEqual(format_eta(None), "예상 시간 계산 중…")


if __name__ == "__main__":
    unittest.main()
