"""Threshold calibration from a labelled set: accuracy per confidence bucket."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Bucket:
    low: float
    high: float
    total: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass
class CalibrationReport:
    total: int = 0
    correct: int = 0
    buckets: list[Bucket] = field(default_factory=list)
    suggested_t_auto: float | None = None
    suggested_t_ask: float | None = None

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


def _bucket(report: CalibrationReport, confidence: float) -> Bucket:
    index = min(int(confidence * 10), 9)
    low = index / 10
    while len(report.buckets) <= index:
        start = len(report.buckets)
        report.buckets.append(Bucket(start / 10, (start + 1) / 10))
    return report.buckets[index]


def calibrate(router, samples: list[dict]) -> CalibrationReport:
    """samples: [{"text": str, "agent": str|None}]. expected None means "not a task"."""
    report = CalibrationReport()
    for sample in samples:
        result = router.classify(sample["text"])
        predicted = result.agent if result.is_task else None
        report.total += 1
        if predicted == sample.get("agent"):
            report.correct += 1
        bucket = _bucket(report, result.confidence)
        bucket.total += 1
        if predicted == sample.get("agent"):
            bucket.correct += 1

    def first_threshold(min_accuracy: float, min_samples: int) -> float | None:
        """Lowest bucket boundary such that everything at or above it meets the target."""
        cumulative_total = 0
        cumulative_correct = 0
        answer = None
        for bucket in sorted(report.buckets, key=lambda item: -item.low):
            if bucket.total == 0:
                continue
            cumulative_total += bucket.total
            cumulative_correct += bucket.correct
            if cumulative_total >= min_samples and cumulative_correct / cumulative_total >= min_accuracy:
                answer = bucket.low
        return answer

    report.suggested_t_auto = first_threshold(0.95, 10)
    report.suggested_t_ask = first_threshold(0.80, 10)
    return report
