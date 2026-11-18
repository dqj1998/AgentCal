import tempfile
import unittest
from pathlib import Path

import agentcal.agents.x_publisher as publisher
from agentcal.policy import revoke_domain, trust_domain

from tests.helpers import make_env
from agentcal.agents.x_publisher import XPublisherAgent
from agentcal.runner.protocol import AgentError


class FakeSession:
    def __init__(self):
        self.calls = []
        self.text = ""

    def goto(self, url):
        self.calls.append(("goto", url))

    def type_into(self, selector, text):
        self.calls.append(("type", selector))
        self.text = text

    def read_text(self, selector):
        return self.text

    def click_or_ground(self, selector, instruction):
        self.calls.append(("click", selector))

    def click_selector(self, selector):
        self.calls.append(("click", selector))

    def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait", selector))


class PublisherTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        original = publisher.make_session
        publisher.make_session = lambda settings: self.session
        self.addCleanup(setattr, publisher, "make_session", original)
        self.agent = XPublisherAgent({})
        self.task = {"prompt": "hello world", "title": "post"}

    def _run(self, steps=None):
        for step in steps or self.agent.steps(self.task):
            self.agent.perform(step, {"task": self.task})

    def test_steps_order(self):
        kinds = [step.kind for step in self.agent.steps(self.task)]
        self.assertEqual(kinds, ["goto", "type_text", "verify_text", "click", "wait_optional"])

    def test_domain_allowlist_requests_approval_for_foreign_url(self):
        agent = XPublisherAgent({"allowed_domains": ["internal.example"]})
        with self.assertRaises(AgentError) as ctx:
            agent.steps(self.task)
        self.assertEqual(ctx.exception.error_class, "human_required")
        self.assertIn("new_domain:x.com", str(ctx.exception))

    def test_domain_allowlist_accepts_its_own_site(self):
        agent = XPublisherAgent({"allowed_domains": ["x.com"]})
        self.assertEqual([s.kind for s in agent.steps(self.task)][0], "goto")

    def test_empty_allowlist_also_asks_a_human(self):
        agent = XPublisherAgent({"allowed_domains": []})
        with self.assertRaises(AgentError) as ctx:
            agent.steps(self.task)
        self.assertEqual(ctx.exception.error_class, "human_required")

    def test_happy_path_publishes_and_confirms(self):
        self._run()
        self.assertEqual(self.session.calls[0][0], "goto")
        self.assertEqual([call[0] for call in self.session.calls], ["goto", "type", "click", "wait"])

    def test_content_mismatch_blocks_before_publish(self):
        self.session.read_text = lambda selector: "different"
        steps = self.agent.steps(self.task)[:3]
        with self.assertRaises(AgentError) as ctx:
            self._run(steps)
        self.assertEqual(ctx.exception.error_class, "human_required")
        self.assertNotIn("click", [call[0] for call in self.session.calls])

    def test_missing_confirmation_toast_is_tolerated(self):
        def boom(selector, timeout_ms):
            raise TimeoutError("no toast")

        self.session.wait_for = boom
        self._run()
        self.assertIn(("click", "[data-testid='tweetButtonInline']"), self.session.calls)

    def test_text_too_long_is_policy_error(self):
        self.task["prompt"] = "x" * 400
        with self.assertRaises(AgentError) as ctx:
            self.agent.steps(self.task)
        self.assertEqual(ctx.exception.error_class, "policy")

    def test_empty_text_is_policy_error(self):
        self.task["prompt"] = "  "
        with self.assertRaises(AgentError) as exc:
            self.agent.steps(self.task)
        self.assertEqual(exc.exception.error_class, "policy")


class PublisherDomainTrustTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.settings = {
            "allowed_domains": ["x.com"],
            "db_path": self.cfg.core.db_path,
            "x_publisher": {"compose_url": "https://medium.com/new-story"},
        }

    def test_unknown_domain_requests_approval(self):
        agent = XPublisherAgent(self.settings)
        with self.assertRaises(AgentError) as ctx:
            agent.steps({"prompt": "hello"})
        self.assertEqual(ctx.exception.error_class, "human_required")
        self.assertIn("new_domain:medium.com", str(ctx.exception))

    def test_trusted_domain_passes_without_config_change(self):
        trust_domain(self.conn, "medium.com", "approval", "2026-01-01T00:00:00Z")
        agent = XPublisherAgent(self.settings)
        self.assertEqual([s.kind for s in agent.steps({"prompt": "hello"})][0], "goto")

    def test_revoked_domain_blocks_again(self):
        trust_domain(self.conn, "medium.com", "approval", "2026-01-01T00:00:00Z")
        revoke_domain(self.conn, "medium.com")
        with self.assertRaises(AgentError):
            XPublisherAgent(self.settings).steps({"prompt": "hello"})


class MultiSiteTest(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "allowed_domains": ["medium.com", "x.com"],
            "sites": {
                "medium": {
                    "compose_url": "https://medium.com/new-story",
                    "text_selector": "#editor",
                    "publish_selector": "#publish",
                    "confirm_selector": "#done",
                    "max_chars": 20000,
                }
            },
            "x_publisher": {"compose_url": "https://x.com/compose/post"},
        }

    def test_selects_by_intent_site(self):
        steps = XPublisherAgent(self.settings).steps({"prompt": "hello", "intent": {"site": "medium"}})
        self.assertEqual(steps[0].target, "https://medium.com/new-story")

    def test_selects_by_host_in_prompt(self):
        steps = XPublisherAgent(self.settings).steps({"prompt": "post this to medium.com", "intent": {}})
        self.assertEqual(steps[0].target, "https://medium.com/new-story")

    def test_unknown_site_name_is_rejected(self):
        with self.assertRaises(AgentError) as ctx:
            XPublisherAgent(self.settings).steps({"prompt": "x", "intent": {"site": "tumblr"}})
        self.assertEqual(ctx.exception.error_class, "policy")
        self.assertIn("site_not_configured", str(ctx.exception))

    def test_no_match_falls_back_to_default_site(self):
        steps = XPublisherAgent(self.settings).steps({"prompt": "hello", "intent": {}})
        self.assertEqual(steps[0].target, "https://x.com/compose/post")
