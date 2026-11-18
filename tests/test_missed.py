import tempfile
import unittest
from pathlib import Path

from agentcal.fsm import Fsm

from tests.helpers import FakeClock, insert_task, make_env


class MissedTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_t6_run_late_policy_runs_missed_task(self):
        cfg, conn = make_env(self.dir)
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(conn, cfg, due="2026-01-01T06:00:00Z")
        report = fsm.reconcile()
        self.assertEqual(report.missed, 1)
        self.assertEqual(len(fsm.claim_due(1)), 1)
        self.assertEqual(conn.execute("SELECT state FROM tasks").fetchone()["state"], "claimed")

    def test_t6_ask_policy_blocks_until_approved(self):
        cfg, conn = make_env(self.dir / "ask", missed_policy="ask")
        clock = FakeClock()
        fsm = Fsm(conn, cfg, clock)
        insert_task(conn, cfg, due="2026-01-01T06:00:00Z", event_id=None)
        fsm.reconcile()
        task = conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(task["state"], "blocked_human")
        self.assertEqual(
            conn.execute("SELECT reason FROM approvals").fetchone()["reason"], "missed"
        )
        self.assertTrue(fsm.resolve_approval(task["id"], "approve", "me"))
        self.assertEqual(conn.execute("SELECT state FROM tasks").fetchone()["state"], "pending")
        self.assertEqual(len(fsm.claim_due(1)), 1)
