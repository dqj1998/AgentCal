"""Parent-side runners: subprocess (default) and in-process (tests/diagnostics)."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator


class RunnerError(RuntimeError):
    pass


class RunnerTimeout(RunnerError):
    pass


def _package_root() -> Path:
    import agentcal

    return Path(agentcal.__file__).resolve().parent.parent


class WorkerRunner:
    """Spawns agentcal.runner.worker; streams events; kills the child on exit."""

    def __init__(self, timeout_s: float = 120.0, python: str | None = None):
        self.timeout_s = timeout_s
        self.python = python or sys.executable

    def run(self, request: dict) -> Iterator[dict]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_package_root()) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [self.python, "-m", "agentcal.runner.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        lines: queue.Queue = queue.Queue()

        def pump() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            while True:
                try:
                    line = lines.get(timeout=self.timeout_s)
                except queue.Empty as exc:
                    raise RunnerTimeout(f"worker silent for {self.timeout_s}s") from exc
                if line is None:
                    stderr = proc.stderr.read() if proc.stderr else ""
                    raise RunnerError(f"worker exited without done: {(stderr or '')[-500:]}")
                event = json.loads(line)
                yield event
                if event.get("event") == "done":
                    return
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass


class LocalRunner:
    """Same protocol, in-process. Used by unit tests and offline diagnostics."""

    def __init__(self, agents: dict):
        self.agents = agents

    def run(self, request: dict) -> Iterator[dict]:
        from .protocol import AgentError

        agent = self.agents[request["agent"]]
        task = request.get("task", {})
        try:
            steps = agent.steps(task)
        except AgentError as exc:
            yield {"event": "done", "ok": False, "error_class": exc.error_class, "detail": str(exc)}
            return
        yield {"event": "steps", "steps": [{"kind": s.kind, "target": s.target} for s in steps]}
        for idx, step in enumerate(steps):
            yield {"event": "step", "idx": idx, "state": "started"}
            try:
                output = agent.perform(
                    step,
                    {"task": task, "run_id": request["run_id"], "settings": request.get("settings", {})},
                )
            except AgentError as exc:
                yield {"event": "step", "idx": idx, "state": "failed", "error_class": exc.error_class}
                yield {"event": "done", "ok": False, "error_class": exc.error_class, "detail": str(exc)}
                return
            except Exception as exc:  # noqa: BLE001
                yield {"event": "step", "idx": idx, "state": "failed", "error_class": "transient"}
                yield {
                    "event": "done",
                    "ok": False,
                    "error_class": "transient",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
                return
            yield {"event": "step", "idx": idx, "state": "done", "output_ref": output}
        yield {"event": "done", "ok": True}
