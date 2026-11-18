import unittest
from datetime import datetime, timezone

from agentcal.envelope import (
    PayloadTooLarge,
    canonical_json,
    merge_description,
    parse_description,
    render_description,
    sign,
    verify,
)

from tests.helpers import SECRET

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
ENV = {"v": 1, "tid": "t1", "nonce": "n", "exp": "2026-12-01T00:00:00Z", "agent": "noop", "payload_ref": "vault:t1"}


class EnvelopeTest(unittest.TestCase):
    def test_sign_verify_roundtrip(self):
        ok, reason = verify(SECRET, sign(SECRET, ENV), NOW, expected_tid="t1")
        self.assertTrue(ok, reason)

    def test_tampered_payload_detected(self):
        signed = sign(SECRET, ENV)
        signed["agent"] = "evil"
        ok, reason = verify(SECRET, signed, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "bad_signature")

    def test_expired(self):
        ok, reason = verify(SECRET, sign(SECRET, dict(ENV, exp="2025-01-01T00:00:00Z")), NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "expired")

    def test_tid_mismatch(self):
        ok, reason = verify(SECRET, sign(SECRET, ENV), NOW, expected_tid="other")
        self.assertFalse(ok)
        self.assertEqual(reason, "tid_mismatch")

    def test_missing_signature(self):
        env = dict(ENV)
        env.pop("nonce")
        ok, reason = verify(SECRET, env, NOW)
        self.assertFalse(ok)
        self.assertEqual(reason, "missing_nonce")

    def test_canonical_json_is_key_order_independent(self):
        self.assertEqual(canonical_json({"b": 1, "a": 2}), canonical_json({"a": 2, "b": 1}))


class DescriptionTest(unittest.TestCase):
    def test_merge_keeps_prompt_and_params(self):
        existing = render_description("human prompt", "old log", "PAYLOAD")
        merged = merge_description(existing, log="state: success")
        prompt, log, params = parse_description(merged)
        self.assertEqual(prompt, "human prompt")
        self.assertEqual(log, "state: success")
        self.assertEqual(params, "PAYLOAD")

    def test_plain_description_becomes_prompt(self):
        prompt, log, params = parse_description("just a human note")
        self.assertEqual(prompt, "just a human note")
        self.assertEqual(log, "")
        self.assertEqual(params, "")

    def test_truncates_log_before_failing(self):
        existing = render_description("p", chr(10).join(f"line{i}" for i in range(400)), "x" * 200)
        merged = merge_description(existing, budget=1200)
        self.assertLessEqual(len(merged), 1200)
        prompt, _log, params = parse_description(merged)
        self.assertEqual(prompt, "p")
        self.assertEqual(params, "x" * 200)

    def test_raises_when_prompt_and_params_exceed_budget(self):
        with self.assertRaises(PayloadTooLarge):
            merge_description(render_description("p" * 2000, "", "x" * 2000), budget=1200)
