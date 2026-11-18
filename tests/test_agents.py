import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentcal.agents.security_scan import SecurityScanAgent
from agentcal.runner.protocol import AgentError

from tests.helpers import FakeClock, make_env


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/boom"):
            self.send_response(503)
            self.end_headers()
            return
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


class SecurityScanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _agent(self, tmp, **overrides):
        settings = {
            "allowed_domains": ["127.0.0.1"],
            "artifacts_dir": str(tmp),
            "db_path": str(Path(tmp) / "a.db"),
        }
        settings.update(overrides)
        from agentcal.db import connect, migrate

        conn = connect(settings["db_path"])
        migrate(conn)
        conn.close()
        return SecurityScanAgent(settings)

    def test_target_outside_allowlist_is_policy_error(self):
        agent = SecurityScanAgent({"allowed_domains": ["internal.example"], "artifacts_dir": "/tmp"})
        with self.assertRaises(AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "fetch", "target": f"http://127.0.0.1:{self.port}/"})(), {"run_id": "r"})
        self.assertEqual(ctx.exception.error_class, "policy")

    def test_fetch_records_finding_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            step = type("S", (), {"kind": "fetch", "target": f"http://127.0.0.1:{self.port}/"})()
            path = agent.perform(step, {"run_id": "run_1"})
            self.assertTrue(Path(path).exists())
            self.assertIn('"status": 200', Path(path).read_text())

    def test_5xx_is_transient(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = self._agent(tmp)
            step = type("S", (), {"kind": "fetch", "target": f"http://127.0.0.1:{self.port}/boom"})()
            with self.assertRaises(AgentError) as ctx:
                agent.perform(step, {"run_id": "run_1"})
            self.assertEqual(ctx.exception.error_class, "transient")

    def test_rate_limit_blocks_second_fetch_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, conn = make_env(Path(tmp))
            agent = self._agent(tmp, max_actions_per_min_per_domain=1)
            step = type("S", (), {"kind": "fetch", "target": f"http://127.0.0.1:{self.port}/"})()
            agent.perform(step, {"run_id": "run_1"})
            with self.assertRaises(AgentError) as ctx:
                agent.perform(step, {"run_id": "run_1"})
            self.assertIn("rate_limited", str(ctx.exception))

    def test_steps_extract_urls_from_prompt(self):
        agent = SecurityScanAgent({"allowed_domains": ["127.0.0.1"]})
        steps = agent.steps({"title": "scan", "prompt": f"check http://127.0.0.1:{self.port}/health please"})
        self.assertEqual(len(steps), 1)
        self.assertTrue(steps[0].target.endswith("/health"))
