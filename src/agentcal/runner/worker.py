"""Subprocess entry point: JSON lines in, JSON lines out. Owns no database writes."""
from __future__ import annotations

import json
import sys

from ..agents import build_agent
from .protocol import AgentError


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    line = sys.stdin.readline()
    if not line:
        return 2
    request = json.loads(line)
    agent = build_agent(request["agent"], request.get("settings", {}))
    task = request.get("task", {})
    try:
        steps = agent.steps(task)
    except AgentError as exc:
        _emit({"event": "done", "ok": False, "error_class": exc.error_class, "detail": str(exc)})
        return 0
    _emit({"event": "steps", "steps": [{"kind": s.kind, "target": s.target} for s in steps]})
    for idx, step in enumerate(steps):
        _emit({"event": "step", "idx": idx, "state": "started"})
        try:
            output = agent.perform(
                step,
                {
                    "task": task,
                    "run_id": request["run_id"],
                    "settings": request.get("settings", {}),
                },
            )
        except AgentError as exc:
            _emit({"event": "step", "idx": idx, "state": "failed", "error_class": exc.error_class})
            _emit({"event": "done", "ok": False, "error_class": exc.error_class, "detail": str(exc)})
            return 0
        except Exception as exc:  # noqa: BLE001 - worker must always answer
            _emit({"event": "step", "idx": idx, "state": "failed", "error_class": "transient"})
            _emit(
                {
                    "event": "done",
                    "ok": False,
                    "error_class": "transient",
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
            return 0
        _emit({"event": "step", "idx": idx, "state": "done", "output_ref": output})
    _emit({"event": "done", "ok": True})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
