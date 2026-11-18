import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from agentcal.agents import NullAgent
from agentcal.agents.security_scan import SecurityScanAgent
from agentcal.browser.profile import ProfileLease
from agentcal.executor import Executor
from agentcal.fsm import Fsm
from agentcal.notifier import TelegramNotifier
from agentcal.runner.protocol import Step
from agentcal.runner.rpc import LocalRunner
from agentcal.telegram import TelegramClient
from agentcal.vault import Vault

from tests.helpers import VAULT_KEY, FakeClock, insert_task, make_env
from tests.test_telegram import FakeAPI


class HumanAgent(NullAgent):
    name = "needs-human"

    def steps(self, task):
        return [Step("probe")]

    def perform(self, step, ctx):
        from agentcal.runner.protocol import AgentError

        raise AgentError("human_required", "captcha")


class PublishAgent(NullAgent):
    name = "publisher"

    def steps(self, task):
        return [Step("publish", "x.com")]

    def perform(self, step, ctx):
        marker = Path(ctx["settings"]["artifacts_dir"]) / ctx["run_id"] / "publish_intent.json"
        if not marker.exists():
            raise AssertionError("publish_intent must exist before the side effect")
        return None


class AcceptanceM1(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_t5_notification_burst_respects_chat_rate(self):
        cfg, conn = make_env(self.dir)
        cfg = replace(cfg, policy=replace(cfg.policy, allowed_users=("42",), allowed_chats=("-100",)))
        api = FakeAPI()
        client = TelegramClient(httpx.Client(transport=httpx.MockTransport(api)), "token")
        clock = FakeClock()
        notifier = TelegramNotifier(conn, client, cfg, clock)
        for index in range(10):
            task_id = insert_task(conn, cfg, task_id=f"tsk_{index}", event_id=None, state="success")
            conn.execute(
                "INSERT INTO notifications(id, task_id, kind, attempts, next_attempt_at, last_error) "
                "VALUES(?,?,?,0,?,NULL)",
                (f"ntf_{index}", task_id, "success", "2026-01-01T09:00:00Z"),
            )
        for _ in range(10):
            notifier.flush()
            clock.advance(3600)
        sends = [name for name in api.methods() if name == "sendMessage"]
        self.assertEqual(len(sends), 10)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 0)

    def test_t7_domain_outside_allowlist_fails_with_policy(self):
        cfg, conn = make_env(self.dir)
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(
            conn, cfg, task_id="tsk_bad", event_id=None, agent="security-scan",
            intent={"targets": ["http://evil.example/"]},
        )
        runner = LocalRunner({"security-scan": SecurityScanAgent({"allowed_domains": ["internal.example"]})})
        executor = Executor(conn, cfg, fsm, clock, runner=runner, vault=Vault(VAULT_KEY))
        executor.run_due(1)
        task = conn.execute("SELECT * FROM tasks WHERE id='tsk_bad'").fetchone()
        self.assertEqual(task["state"], "failed")
        self.assertTrue(conn.execute("SELECT exit_reason FROM runs").fetchone()["exit_reason"].startswith("policy"))

    def test_t9_profile_busy_releases_and_requeues(self):
        cfg, conn = make_env(self.dir)
        cfg = replace(cfg, core=replace(cfg.core, profile_wait_s=0.2))
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(conn, cfg, task_id="tsk_p", event_id=None, agent="x-publisher")
        runner = LocalRunner({"x-publisher": NullAgent({})})
        executor = Executor(conn, cfg, fsm, clock, runner=runner, vault=Vault(VAULT_KEY))
        with ProfileLease(cfg.browser.profile_dir, wait_s=1):
            executor.run_due(1)
        task = conn.execute("SELECT * FROM tasks WHERE id='tsk_p'").fetchone()
        self.assertEqual(task["state"], "pending")
        self.assertEqual(task["attempt"], 1)
        self.assertIn("profile", conn.execute("SELECT exit_reason FROM runs").fetchone()["exit_reason"])
        clock.advance(31)
        executor.run_due(1)
        self.assertEqual(
            conn.execute("SELECT state FROM tasks WHERE id='tsk_p'").fetchone()["state"], "success"
        )

    def test_publish_intent_is_written_before_the_side_effect(self):
        cfg, conn = make_env(self.dir)
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(conn, cfg, task_id="tsk_pub", event_id=None, agent="publisher")
        executor = Executor(
            conn, cfg, fsm, clock, runner=LocalRunner({"publisher": PublishAgent({})}), vault=Vault(VAULT_KEY)
        )
        executor.run_due(1)
        self.assertEqual(
            conn.execute("SELECT state FROM tasks WHERE id='tsk_pub'").fetchone()["state"], "success"
        )
        kinds = [row["kind"] for row in conn.execute("SELECT kind FROM artifacts")]
        self.assertIn("publish_intent", kinds)

    def test_human_requirement_blocks_and_notifies(self):
        cfg, conn = make_env(self.dir)
        cfg = replace(cfg, policy=replace(cfg.policy, allowed_users=("42",), allowed_chats=("-100",)))
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(conn, cfg, task_id="tsk_h", event_id=None, agent="needs-human")
        executor = Executor(
            conn, cfg, fsm, clock, runner=LocalRunner({"needs-human": HumanAgent({})}), vault=Vault(VAULT_KEY)
        )
        executor.run_due(1)
        task = conn.execute("SELECT * FROM tasks WHERE id='tsk_h'").fetchone()
        self.assertEqual(task["state"], "blocked_human")
        self.assertEqual(
            conn.execute("SELECT reason FROM approvals").fetchone()["reason"], "captcha"
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0], 1
        )
