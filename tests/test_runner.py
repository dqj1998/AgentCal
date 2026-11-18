import tempfile
import unittest
from pathlib import Path

from agentcal.agents import NullAgent
from agentcal.runner.rpc import LocalRunner, WorkerRunner

REQUEST = {
    "run_id": "run_test",
    "agent": "noop",
    "task": {"id": "tsk_1", "title": "t", "prompt": "p", "intent": {}},
    "settings": {},
}


class WorkerRunnerTest(unittest.TestCase):
    def test_subprocess_worker_streams_steps_and_done(self):
        events = list(WorkerRunner(timeout_s=20).run(REQUEST))
        self.assertEqual(events[0]["event"], "steps")
        self.assertEqual([s["kind"] for s in events[0]["steps"]], ["prepare", "execute", "finalize"])
        self.assertEqual(events[-1], {"event": "done", "ok": True})
        self.assertEqual(sum(1 for e in events if e["event"] == "step" and e["state"] == "done"), 3)

    def test_local_runner_matches_protocol_shape(self):
        events = list(LocalRunner({"noop": NullAgent({})}).run(REQUEST))
        self.assertEqual(events[0]["event"], "steps")
        self.assertEqual(events[-1]["event"], "done")
