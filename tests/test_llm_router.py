import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from agentcal.llm.router import LlmRouter, LlmUnavailable

from tests.helpers import FakeClock, make_env


class FakeOllama:
    def __init__(self, is_task=True, agent="security-scan", votes=None, bad_json=False, status=200):
        self.is_task = is_task
        self.agent = agent
        self.votes = list(votes or [])
        self.bad_json = bad_json
        self.status = status
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        required = payload["format"].get("required", [])
        self.calls.append(required)
        if self.status >= 400:
            return httpx.Response(self.status, json={})
        if self.bad_json:
            content = "not json"
        elif "is_task" in required:
            content = json.dumps({"is_task": self.is_task})
        else:
            agent = self.votes.pop(0) if self.votes else self.agent
            content = json.dumps({"agent": agent})
        return httpx.Response(
            200,
            json={
                "message": {"content": content},
                "prompt_eval_count": 100,
                "eval_count": 20,
            },
        )


class LlmRouterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.clock = FakeClock()

    def _router(self, api, samples=3, **llm_overrides):
        if llm_overrides:
            self.cfg = replace(self.cfg, llm=replace(self.cfg.llm, **llm_overrides))
        return LlmRouter(
            httpx.Client(transport=httpx.MockTransport(api)),
            self.cfg,
            self.conn,
            self.clock,
            base_url="http://127.0.0.1:11434",
            samples=samples,
        )

    def test_classifies_task_and_agent(self):
        result = self._router(FakeOllama()).classify("巡检一下内网", self.clock())
        self.assertTrue(result.is_task)
        self.assertEqual(result.agent, "security-scan")
        self.assertEqual(result.confidence, 1.0)

    def test_non_task_text_is_filtered(self):
        result = self._router(FakeOllama(is_task=False)).classify("今天心情不错", self.clock())
        self.assertFalse(result.is_task)
        self.assertIsNone(result.agent)

    def test_disagreement_lowers_confidence_below_threshold(self):
        api = FakeOllama(votes=["security-scan", "noop", "security-scan", "noop"])
        result = self._router(api, samples=4).classify("do something", self.clock())
        self.assertTrue(result.is_task)
        self.assertLess(result.confidence, self.cfg.llm.t_ask)
        self.assertEqual(result.source, "router_low_confidence")
        self.assertIsNone(result.agent)

    def test_invalid_json_never_becomes_a_task(self):
        result = self._router(FakeOllama(bad_json=True)).classify("do something", self.clock())
        self.assertFalse(result.is_task)
        self.assertEqual(result.confidence, 0.0)

    def test_unreachable_endpoint_raises(self):
        with self.assertRaises(LlmUnavailable):
            self._router(FakeOllama(status=500)).classify("do something", self.clock())

    def test_cost_is_recorded(self):
        router = self._router(FakeOllama(), price_in_per_1k=1.0, price_out_per_1k=2.0)
        router.classify("巡检", self.clock())
        row = self.conn.execute("SELECT COUNT(*) AS n, SUM(cost_usd) AS c FROM llm_calls").fetchone()
        self.assertEqual(row["n"], 6)
        self.assertAlmostEqual(row["c"], 6 * (0.1 + 0.04), places=6)
