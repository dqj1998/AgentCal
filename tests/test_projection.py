import tempfile
import unittest
from pathlib import Path

from agentcal.envelope import parse_description
from agentcal.projector import CalendarProjector

from tests.helpers import (
    FakeCalendar,
    FakeClock,
    description,
    enqueue_projection,
    insert_task,
    make_env,
    make_event,
)


class ProjectionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.cfg, self.conn = make_env(self.dir)
        self.clock = FakeClock()
        self.cal = FakeCalendar(self.clock)
        self.projector = CalendarProjector(self.conn, self.cal, self.cfg, self.clock)

    def _prepared_task(self, event_id="evt1", state="success"):
        self.cal.add(make_event(event_id, desc=description("human prompt", log="old log")))
        insert_task(self.conn, self.cfg, event_id=event_id, state=state)
        enqueue_projection(self.conn, "tsk_manual")

    def test_t3_projection_preserves_human_prompt_and_params(self):
        self._prepared_task()
        self.assertEqual(self.projector.flush(), 1)
        event = self.cal.events["evt1"]
        prompt, log, params = parse_description(event["description"])
        self.assertEqual(prompt, "human prompt")
        self.assertIn("state: success", log)
        self.assertEqual(params, parse_description(description())[2])
        self.assertEqual(event["colorId"], "10")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM projection_outbox").fetchone()[0], 0
        )

    def test_412_is_retried_and_succeeds(self):
        self._prepared_task()
        self.cal.patch_conflicts_remaining = 1
        self.assertEqual(self.projector.flush(), 1)
        self.assertEqual(self.cal.calls["patch"], 2)
        self.assertEqual(self.cal.events["evt1"]["colorId"], "10")

    def test_conflict_exhaustion_defers_to_outbox(self):
        self._prepared_task()
        self.cal.patch_conflicts_remaining = 10
        self.assertEqual(self.projector.flush(), 0)
        row = self.conn.execute("SELECT * FROM projection_outbox").fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], "2026-01-01T09:00:00Z")

    def test_task_without_event_is_a_noop(self):
        insert_task(self.conn, self.cfg, event_id=None)
        enqueue_projection(self.conn, "tsk_manual")
        self.assertEqual(self.projector.flush(), 1)

    def test_projection_failure_never_touches_task_state(self):
        self._prepared_task()
        self.conn.execute("DELETE FROM projected_events")
        self.cal.events.clear()
        self.assertEqual(self.projector.flush(), 0)
        self.assertEqual(
            self.conn.execute("SELECT state FROM tasks").fetchone()["state"], "success"
        )
