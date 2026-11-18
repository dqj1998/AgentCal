import unittest

import httpx

import agentcal.agents.hashnode as hn


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class HashnodeTest(unittest.TestCase):
    def setUp(self):
        self.agent = hn.HashnodeAgent({"hashnode_token": "tok", "publication_id": "pub1"})
        self.task = {"title": "Title", "prompt": "body"}

    def _patch(self, handler):
        original = hn._http_client
        hn._http_client = lambda: client_for(handler)
        self.addCleanup(setattr, hn, "_http_client", original)

    def test_missing_token_is_policy(self):
        agent = hn.HashnodeAgent({})
        agent.token = None
        with self.assertRaises(hn.AgentError) as ctx:
            agent.steps(self.task)
        self.assertEqual(ctx.exception.error_class, "policy")

    def test_empty_text_is_policy(self):
        with self.assertRaises(hn.AgentError):
            self.agent.steps({"title": "T", "prompt": "  "})

    def test_publish_returns_url(self):
        def handler(request):
            body = request.content.decode()
            if "publishPost" in body:
                return httpx.Response(200, json={"data": {"publishPost": {"post": {"url": "https://x.hashnode.dev/p"}}}})
            return httpx.Response(200, json={"data": {"me": {"publications": {"edges": []}}}})

        self._patch(handler)
        steps = self.agent.steps(self.task)
        self.agent.perform(steps[0], {"task": self.task})
        self.assertEqual(self.agent.perform(steps[1], {"task": self.task}), "https://x.hashnode.dev/p")

    def test_publication_is_discovered_when_not_configured(self):
        agent = hn.HashnodeAgent({"hashnode_token": "tok"})
        seen = {}

        def handler(request):
            if "query Me" in request.content.decode():
                return httpx.Response(200, json={"data": {"me": {"publications": {"edges": [{"node": {"id": "pub9", "title": "T"}}]}}}})
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"data": {"publishPost": {"post": {"url": "https://x/p"}}}})

        self._patch(handler)
        agent.perform(type("S", (), {"kind": "publication", "target": None})(), {"task": self.task})
        self.assertEqual(agent.publication_id, "pub9")
        agent.perform(type("S", (), {"kind": "publish", "target": None})(), {"task": self.task})
        self.assertIn("pub9", seen["body"])

    def test_redirected_api_is_paid_only(self):
        agent = hn.HashnodeAgent({"hashnode_token": "tok"})
        self._patch(lambda request: httpx.Response(301, headers={"Location": "https://hashnode.com/changelog"}))
        with self.assertRaises(hn.AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "publication", "target": None})(), {"task": self.task})
        self.assertEqual(ctx.exception.error_class, "policy")
        self.assertIn("paid_only", str(ctx.exception))

    def test_graphql_errors_are_policy(self):
        agent = hn.HashnodeAgent({"hashnode_token": "tok"})
        self._patch(lambda request: httpx.Response(200, json={"errors": [{"message": "Unauthorized"}]}))
        with self.assertRaises(hn.AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "publication", "target": None})(), {"task": self.task})
        self.assertEqual(ctx.exception.error_class, "policy")

    def test_http_402_is_policy(self):
        agent = hn.HashnodeAgent({"hashnode_token": "tok"})
        self._patch(lambda request: httpx.Response(402, json={}))
        with self.assertRaises(hn.AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "publication", "target": None})(), {"task": self.task})
        self.assertEqual(ctx.exception.error_class, "policy")
