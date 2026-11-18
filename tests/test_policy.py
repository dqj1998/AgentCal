import tempfile
import unittest
from pathlib import Path

from agentcal.policy import evaluate_calendar

from tests.helpers import CAL_ID, OWNER, make_config


class CalendarPolicyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = make_config(Path(tempfile.mkdtemp()))

    def _event(self, organizer, creator=None, status="confirmed"):
        event = {"organizer": {"email": organizer}, "status": status}
        if creator:
            event["creator"] = {"email": creator}
        return event

    def test_owner_is_trusted(self):
        verdict = evaluate_calendar(self._event(OWNER), self.cfg, signature_ok=False)
        self.assertTrue(verdict.allowed)

    def test_secondary_calendar_organizer_with_owner_creator_is_trusted(self):
        verdict = evaluate_calendar(
            self._event(CAL_ID, creator=OWNER), self.cfg, signature_ok=False
        )
        self.assertTrue(verdict.allowed)

    def test_secondary_calendar_event_from_someone_else_is_rejected(self):
        verdict = evaluate_calendar(
            self._event(CAL_ID, creator="intruder@example.com"), self.cfg, signature_ok=False
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "not_owner")

    def test_foreign_organizer_is_rejected(self):
        verdict = evaluate_calendar(
            self._event("other@example.com", creator=OWNER), self.cfg, signature_ok=False
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "not_owner")

    def test_per_agent_domains_override_global(self):
        from dataclasses import replace

        from agentcal.config import AgentConfig
        from agentcal.policy import domains_for, host_allowed

        cfg = replace(
            self.cfg,
            policy=replace(self.cfg.policy, allowed_domains=("global.example",)),
            agents={"security-scan": AgentConfig(domains=("intranet.example",))},
        )
        self.assertEqual(domains_for("security-scan", cfg), ("intranet.example",))
        self.assertEqual(domains_for("x-publisher", cfg), ("global.example",))
        self.assertTrue(host_allowed("http://a.intranet.example/x", domains_for("security-scan", cfg)))
        self.assertFalse(host_allowed("http://x.com/", domains_for("security-scan", cfg)))
        self.assertTrue(host_allowed("https://x.com/compose/post", ("x.com",)))
        self.assertFalse(host_allowed("https://notx.com/", ("x.com",)))

    def test_params_require_signature(self):
        verdict = evaluate_calendar(
            self._event(OWNER), self.cfg, signature_ok=False, signature_reason="bad_signature", has_params=True
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "bad_signature")
