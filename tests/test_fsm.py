import tempfile
import unittest
from pathlib import Path

from agentcal.fsm import Fsm

from tests.helpers import FakeClock, insert_task, make_env


def count(conn, sql: str, args=()) -> int:
    return conn.execute(sql, args).fetchone()[0]


class FsmTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.clock = FakeClock()
        self.fsm = Fsm(self.conn, self.cfg, self.clock)

    def test_claim_creates_run_and_increments_attempt(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.assertIsNotNone(run)
        self.assertEqual(run["state"], "leased")
        self.assertEqual(self.fsm.task(task_id)["attempt"], 1)
        self.assertIsNone(self.fsm.claim(task_id))

    def test_succeed_sets_success_and_enqueues_projection(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.assertTrue(self.fsm.start(run["id"]))
        self.assertTrue(self.fsm.succeed(run["id"]))
        self.assertEqual(self.fsm.task(task_id)["state"], "success")
        self.assertEqual(count(self.conn, "SELECT COUNT(*) FROM projection_outbox"), 1)

    def test_transient_failure_backs_off_then_retries(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.fail(run["id"], "transient", "timeout")
        row = self.fsm.task(task_id)
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["attempt"], 1)
        self.assertGreater(row["not_before"], row["updated_at"])
        self.assertEqual(self.fsm.claim_due(1), [])
        self.clock.advance(31)
        self.assertEqual(len(self.fsm.claim_due(1)), 1)
        self.assertEqual(self.fsm.task(task_id)["attempt"], 2)

    def test_attempts_exhausted_fails(self):
        task_id = insert_task(self.conn, self.cfg)
        for _ in range(3):
            run = self.fsm.claim(task_id)
            self.fsm.start(run["id"])
            self.fsm.fail(run["id"], "transient", "boom")
            self.clock.advance(600)
        self.assertEqual(self.fsm.task(task_id)["state"], "failed")

    def test_block_then_approve_resumes_run(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.assertTrue(self.fsm.block(run["id"], "captcha"))
        self.assertEqual(self.fsm.task(task_id)["state"], "blocked_human")
        self.assertEqual(self.conn.execute("SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()["state"], "blocked")
        self.assertTrue(self.fsm.resolve_approval(task_id, "approve", "me"))
        self.assertEqual(self.fsm.task(task_id)["state"], "running")
        self.assertEqual(self.conn.execute("SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()["state"], "running")

    def test_deny_cancels(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.block(run["id"], "captcha")
        self.assertTrue(self.fsm.resolve_approval(task_id, "deny", "me"))
        self.assertEqual(self.fsm.task(task_id)["state"], "canceled")

    def test_approval_timeout_fails_task(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.block(run["id"], "captcha")
        self.clock.advance(86401)
        self.fsm.reconcile()
        self.assertEqual(self.fsm.task(task_id)["state"], "failed")

    def test_cancel_kills_active_run(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.assertTrue(self.fsm.cancel(task_id))
        self.assertEqual(self.fsm.task(task_id)["state"], "canceled")
        self.assertEqual(self.conn.execute("SELECT state FROM runs WHERE id=?", (run["id"],)).fetchone()["state"], "failed")

    def test_reconcile_orphan_without_publish_retries(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.conn.execute(
            "INSERT INTO steps(id, run_id, idx, kind, state) VALUES('s1', ?, 0, 'prepare', 'done')", (run["id"],)
        )
        self.clock.advance(91)
        report = self.fsm.reconcile()
        self.assertEqual(report.orphaned, 1)
        self.assertEqual(report.retried, 1)
        self.assertEqual(self.fsm.task(task_id)["state"], "pending")
        self.assertEqual(self.fsm.task(task_id)["attempt"], 1)

    def test_reconcile_orphan_with_publish_blocks_for_unknown_side_effect(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.conn.execute(
            "INSERT INTO steps(id, run_id, idx, kind, state) VALUES('s1', ?, 0, 'publish', 'started')", (run["id"],)
        )
        self.clock.advance(91)
        report = self.fsm.reconcile()
        self.assertEqual(report.blocked, 1)
        task = self.fsm.task(task_id)
        self.assertEqual(task["state"], "blocked_human")
        self.assertEqual(
            self.conn.execute("SELECT reason FROM approvals WHERE task_id=?", (task_id,)).fetchone()["reason"],
            "unknown_side_effect",
        )

    def test_approving_new_domain_persists_trust(self):
        task_id = insert_task(self.conn, self.cfg)
        run = self.fsm.claim(task_id)
        self.fsm.start(run["id"])
        self.fsm.fail(run["id"], "human_required", "new_domain:medium.com")
        self.assertTrue(self.fsm.resolve_approval(task_id, "approve", "me"))
        domains = [row["domain"] for row in self.conn.execute("SELECT domain FROM trusted_domains")]
        self.assertEqual(domains, ["medium.com"])

    def test_cancel_by_event(self):
        task_id = insert_task(self.conn, self.cfg, event_id="evt9")
        self.assertTrue(self.fsm.cancel_by_event("evt9"))
        self.assertEqual(self.fsm.task(task_id)["state"], "canceled")
