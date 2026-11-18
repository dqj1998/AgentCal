import unittest

import httpx

import agentcal.agents.telegraph as telegraph
from agentcal.agents.telegraph import TelegraphAgent
from agentcal.runner.protocol import AgentError


class TelegraphTest(unittest.TestCase):
    def setUp(self):
        self.agent = TelegraphAgent({"telegraph_token": "tok", "author_name": "Dqj"})
        self.task = {"title": "Bottleneck", "prompt": "hello world"}

    def _patch(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        original = telegraph._http_client
        telegraph._http_client = lambda: client
        self.addCleanup(setattr, telegraph, "_http_client", original)

    def test_missing_token_is_policy_error(self):
        agent = TelegraphAgent({"telegraph_token": None})
        agent.token = None
        with self.assertRaises(AgentError) as ctx:
            agent.steps(self.task)
        self.assertEqual(ctx.exception.error_class, "policy")

    def test_create_page_returns_url(self):
        captured = {}

        def handler(request):
            captured["body"] = request.content.decode()
            return httpx.Response(200, json={"ok": True, "result": {"url": "https://telegra.ph/x"}})

        self._patch(handler)
        self.assertEqual(self.agent.perform(self.agent.steps(self.task)[0], {"task": self.task}), "https://telegra.ph/x")
        self.assertIn("Bottleneck", captured["body"])
        self.assertIn("hello+world", captured["body"])

    def test_api_error_is_policy(self):
        self._patch(lambda request: httpx.Response(200, json={"ok": False, "error": "TITLE_REQUIRED"}))
        with self.assertRaises(AgentError) as ctx:
            self.agent.perform(self.agent.steps(self.task)[0], {"task": self.task})
        self.assertEqual(ctx.exception.error_class, "policy")
        self.assertIn("TITLE_REQUIRED", str(ctx.exception))

    def test_5xx_is_transient(self):
        self._patch(lambda request: httpx.Response(503, json={}))
        with self.assertRaises(AgentError) as ctx:
            self.agent.perform(self.agent.steps(self.task)[0], {"task": self.task})
        self.assertEqual(ctx.exception.error_class, "transient")
