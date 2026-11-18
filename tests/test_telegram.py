import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from agentcal.fsm import Fsm
from agentcal.notifier import TelegramNotifier
from agentcal.telegram import (
    CALLBACK_MAX_BYTES,
    ChatRateLimiter,
    TelegramChannel,
    TelegramClient,
    callback_data,
    short_id,
)

from tests.helpers import FakeClock, insert_task, make_env


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.updates = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            result, self.updates = self.updates, []
        elif method in ("sendMessage", "editMessageText"):
            result = {"message_id": 900 + len(self.calls)}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def methods(self):
        return [name for name, _ in self.calls]


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.cfg = replace(
            self.cfg,
            core=replace(self.cfg.core, default_agent="noop"),
            policy=replace(self.cfg.policy, allowed_users=("42",), allowed_chats=("-100",)),
        )
        self.api = FakeAPI()
        self.client = TelegramClient(httpx.Client(transport=httpx.MockTransport(self.api)), "token")
        self.clock = FakeClock()
        self.fsm = Fsm(self.conn, self.cfg, self.clock)
        self.channel = TelegramChannel(self.conn, self.client, self.cfg, self.fsm, self.clock)

    def _message(self, text: str, *, user=42, chat=-100, message_id=7):
        return {
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat},
                "from": {"id": user},
                "text": text,
            },
        }

    def test_callback_data_within_telegram_limit(self):
        data = callback_data("q", "tsk_" + "a" * 20, "a")
        self.assertLessEqual(len(data.encode()), CALLBACK_MAX_BYTES)
        self.assertEqual(len(short_id("tsk_x")), 8)

    def test_authorized_task_creates_command(self):
        self.api.updates = [self._message("/task scan the intranet")]
        self.assertEqual(self.channel.poll(), 1)
        row = self.conn.execute("SELECT * FROM commands").fetchone()
        self.assertEqual(row["source"], "telegram")
        self.assertEqual(row["policy_verdict"], "allow")
        self.assertIn('"agent": "noop"', row["parsed_json"])

    def test_unauthorized_actor_is_dropped(self):
        self.api.updates = [self._message("/task hack", user=99)]
        self.channel.poll()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 0)
        self.assertNotIn("sendMessage", self.api.methods())

    def test_duplicate_message_is_idempotent(self):
        update = self._message("/task once")
        self.api.updates = [update]
        self.channel.poll()
        self.api.updates = [dict(update, update_id=99)]
        self.channel.poll()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0], 1)

    def test_callback_approve_resumes_task(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.block(run["id"], "captcha")
        self.conn.execute(
            "INSERT INTO tg_callbacks(short_id, task_id, created_at) VALUES(?,?,?)",
            (short_id(task_id), task_id, "2026-01-01T09:00:00Z"),
        )
        self.api.updates = [
            {
                "update_id": 11,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": 42},
                    "message": {"chat": {"id": -100}},
                    "data": f"q:{short_id(task_id)}:a",
                },
            }
        ]
        self.channel.poll()
        self.assertEqual(self.conn.execute("SELECT state FROM tasks").fetchone()["state"], "running")
        self.assertIn("answerCallbackQuery", self.api.methods())

    def test_notifier_sends_card_and_registers_callback(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.block(run["id"], "captcha")
        notifier = TelegramNotifier(self.conn, self.client, self.cfg, self.clock)
        self.assertEqual(notifier.flush(), 1)
        sent = [payload for name, payload in self.api.calls if name == "sendMessage"][0]
        self.assertIn("inline_keyboard", sent["reply_markup"])
        data = sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertLessEqual(len(data.encode()), CALLBACK_MAX_BYTES)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM tg_callbacks").fetchone()[0], 1
        )

    def test_domains_command_lists_and_revoke_removes(self):
        from agentcal.policy import trust_domain

        trust_domain(self.conn, "medium.com", "approval", "2026-01-01T00:00:00Z")
        self.api.updates = [self._message("/domains")]
        self.channel.poll()
        sent = [payload for name, payload in self.api.calls if name == "sendMessage"][-1]
        self.assertIn("medium.com", sent["text"])

        self.api.updates = [self._message("/revoke medium.com", message_id=8)]
        self.channel.poll()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM trusted_domains").fetchone()[0], 0
        )

    def test_rate_limiter_skips_within_interval(self):
        limiter = ChatRateLimiter(self.clock, min_interval_s=1.0)
        self.assertTrue(limiter.ok(-100))
        self.assertFalse(limiter.ok(-100))
        self.clock.advance(1.1)
        self.assertTrue(limiter.ok(-100))
