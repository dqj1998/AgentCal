import tempfile
import unittest
from pathlib import Path

from agentcal.fsm import Fsm
from agentcal.poller import CalendarPoller
from agentcal.scheduler import Scheduler
from agentcal.vault import Vault

from tests.helpers import (
    SECRET,
    VAULT_KEY,
    FakeCalendar,
    FakeClock,
    description,
    envelope,
    make_env,
    make_event,
)


class PollerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.clock = FakeClock()
        self.cal = FakeCalendar(self.clock)
        self.fsm = Fsm(self.conn, self.cfg, self.clock)
        self.poller = CalendarPoller(self.conn, self.cal, self.cfg, SECRET, self.fsm, self.clock)
        self.scheduler = Scheduler(self.conn, self.cfg, self.fsm, self.clock, Vault(VAULT_KEY))

    def _full_sync(self):
        self.conn.execute("DELETE FROM projector_state")
        return self.poller.tick()

    def _count(self, table: str) -> int:
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_t1_same_event_three_ticks_creates_one_task(self):
        self.cal.add(make_event())
        for _ in range(3):
            self._full_sync()
        self.assertEqual(self._count("commands"), 1)
        self.assertEqual(self.scheduler.apply_commands(), 1)
        self.assertEqual(self._count("tasks"), 1)
        self.assertEqual(self.scheduler.apply_commands(), 0)

    def test_t4_tampered_signature_is_rejected(self):
        self.cal.add(make_event(desc=description(env=envelope(tamper=True))))
        stats = self.poller.tick()
        self.assertEqual(stats.rejected, 1)
        row = self.conn.execute("SELECT signature_ok, policy_verdict FROM commands").fetchone()
        self.assertEqual(row["signature_ok"], 0)
        self.assertEqual(row["policy_verdict"], "reject:bad_signature")
        self.scheduler.apply_commands()
        self.assertEqual(self._count("tasks"), 0)

    def test_unsigned_event_without_agent_is_rejected(self):
        self.cal.add(make_event(desc="just a human note"))
        self.poller.tick()
        self.assertEqual(
            self.conn.execute("SELECT policy_verdict FROM commands").fetchone()["policy_verdict"],
            "reject:no_agent",
        )

    def test_unsigned_human_event_routes_to_default_agent(self):
        from dataclasses import replace

        self.cfg = replace(self.cfg, core=replace(self.cfg.core, default_agent="noop"))
        self.poller = CalendarPoller(self.conn, self.cal, self.cfg, SECRET, self.fsm, self.clock)
        self.cal.add(make_event(desc="just a human note"))
        self.poller.tick()
        row = self.conn.execute("SELECT policy_verdict, parsed_json FROM commands").fetchone()
        self.assertEqual(row["policy_verdict"], "allow")
        self.assertIn('"agent": "noop"', row["parsed_json"])

    def test_non_owner_is_rejected(self):
        self.cal.add(make_event(organizer="someone@else.com"))
        self.poller.tick()
        self.assertEqual(
            self.conn.execute("SELECT policy_verdict FROM commands").fetchone()["policy_verdict"],
            "reject:not_owner",
        )

    def test_410_resyncs_and_ingests_new_events(self):
        self.cal.add(make_event("evt1"))
        self.poller.tick()
        self.cal.invalid_sync_once = True
        self.cal.add(make_event("evt2", summary="second"))
        stats = self.poller.tick()
        self.assertTrue(stats.resynced)
        self.assertEqual(stats.error, None)
        self.assertEqual(self._count("commands"), 2)

    def test_cancelled_event_cancels_task(self):
        self.cal.add(make_event())
        self.poller.tick()
        self.scheduler.apply_commands()
        self.cal.cancel("evt1")
        self.poller.tick()
        self.assertEqual(self.conn.execute("SELECT state FROM tasks").fetchone()["state"], "canceled")

    def test_self_write_is_skipped(self):
        event = self.cal.add(make_event())
        self.conn.execute(
            "INSERT INTO projected_events(event_id, updated, projected_at) VALUES(?,?,?)",
            ("evt1", event["updated"], "2026-01-01T09:00:00Z"),
        )
        stats = self.poller.tick()
        self.assertEqual(stats.skipped_self, 1)
        self.assertEqual(self._count("commands"), 0)

    def test_offset_datetime_is_normalised_to_utc(self):
        self.cal.add(make_event(start="2026-01-01T17:00:00+09:00"))  # 08:00Z, clock is 09:00Z
        self.poller.tick()
        self.scheduler.apply_commands()
        row = self.conn.execute("SELECT due_at, not_before FROM tasks").fetchone()
        self.assertEqual(row["due_at"], "2026-01-01T08:00:00Z")
        self.assertEqual(len(self.fsm.claim_due(1)), 1)

    def test_rate_limited_tick_is_reported(self):
        self.cal.rate_limit_once = True
        stats = self.poller.tick()
        self.assertTrue(stats.error.startswith("rate_limited"))
        failures = self.conn.execute(
            "SELECT consecutive_failures FROM projector_state WHERE kind='calendar'"
        ).fetchone()["consecutive_failures"]
        self.assertEqual(failures, 1)
