import tempfile
import unittest
from pathlib import Path

from agentcal.agents import NullAgent
from agentcal.envelope import parse_description
from agentcal.executor import Executor
from agentcal.fsm import Fsm
from agentcal.poller import CalendarPoller
from agentcal.projector import CalendarProjector
from agentcal.scheduler import Scheduler
from agentcal.runner.rpc import LocalRunner
from agentcal.supervisor import Supervisor
from agentcal.vault import Vault

from tests.helpers import SECRET, VAULT_KEY, FakeCalendar, FakeClock, make_env, make_event


class SmokeTest(unittest.TestCase):
    def test_calendar_event_reaches_success_and_projects(self):
        tmp = Path(tempfile.mkdtemp())
        cfg, conn = make_env(tmp)
        clock = FakeClock()
        cal = FakeCalendar(clock)
        fsm = Fsm(conn, cfg, clock)
        poller = CalendarPoller(conn, cal, cfg, SECRET, fsm, clock)
        scheduler = Scheduler(conn, cfg, fsm, clock, Vault(VAULT_KEY))
        executor = Executor(
            conn, cfg, fsm, clock, runner=LocalRunner({"noop": NullAgent({})}), vault=Vault(VAULT_KEY)
        )
        projector = CalendarProjector(conn, cal, cfg, clock)
        supervisor = Supervisor(
            conn, cfg, fsm, scheduler, executor, projector, poller=poller, clock=clock
        )

        cal.add(make_event())
        report = supervisor.tick_once()

        self.assertEqual(report.applied, 1)
        self.assertEqual(report.executed, 1)
        task = conn.execute("SELECT * FROM tasks").fetchone()
        self.assertEqual(task["state"], "success")
        steps = conn.execute("SELECT state FROM steps ORDER BY idx").fetchall()
        self.assertEqual([s["state"] for s in steps], ["done", "done", "done"])

        event = cal.events["evt1"]
        prompt, log, _ = parse_description(event["description"])
        self.assertEqual(prompt, "publish weekly note")
        self.assertIn("state: success", log)
        self.assertEqual(event["colorId"], "10")

        second = supervisor.tick_once()
        self.assertGreaterEqual(second.poll["skipped_self"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
