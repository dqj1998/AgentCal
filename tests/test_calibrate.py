import unittest

from agentcal.llm.calibrate import calibrate
from agentcal.llm.router import Classification


class FakeRouter:
    def __init__(self, mapping):
        self.mapping = mapping

    def classify(self, text):
        confidence, agent, is_task = self.mapping[text]
        return Classification(is_task, agent, {}, confidence, "router")


class CalibrateTest(unittest.TestCase):
    def test_thresholds_follow_measured_accuracy(self):
        samples, mapping = [], {}
        for index in range(12):
            text = f"high{index}"
            samples.append({"text": text, "agent": "security-scan"})
            mapping[text] = (0.95, "security-scan", True)
        for index in range(10):
            text = f"mid{index}"
            samples.append({"text": text, "agent": "security-scan"})
            mapping[text] = (0.72, "security-scan" if index < 8 else "noop", True)
        for index in range(10):
            text = f"low{index}"
            samples.append({"text": text, "agent": "security-scan"})
            mapping[text] = (0.45, "security-scan" if index < 2 else "noop", True)
        report = calibrate(FakeRouter(mapping), samples)
        self.assertEqual(report.total, 32)
        self.assertEqual(report.correct, 22)
        self.assertAlmostEqual(report.accuracy, 22 / 32, places=4)
        self.assertEqual(report.suggested_t_auto, 0.9)
        self.assertEqual(report.suggested_t_ask, 0.7)

    def test_non_task_samples_count_as_none_agent(self):
        mapping = {"hi": (0.9, None, False)}
        report = calibrate(FakeRouter(mapping), [{"text": "hi", "agent": None}])
        self.assertEqual(report.correct, 1)
