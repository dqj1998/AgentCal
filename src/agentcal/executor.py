"""Execution of one claimed run through a runner (subprocess by default)."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from pathlib import Path

from .browser.profile import ProfileBusy, ProfileLease
from .clock import iso
from .config import AppConfig
from .fsm import Fsm
from .policy import domains_for
from .runner.protocol import AgentError
from .runner.rpc import LocalRunner, RunnerError, WorkerRunner

log = logging.getLogger(__name__)


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: AppConfig,
        fsm: Fsm,
        clock,
        runner=None,
        vault=None,
    ):
        self.conn = conn
        self.cfg = cfg
        self.fsm = fsm
        self.clock = clock
        self.runner = runner or WorkerRunner()
        self.vault = vault
        self.artifacts_dir = Path(cfg.core.artifacts_dir)

    # ------------------------------------------------------------------ public
    def run_due(self, limit: int = 1) -> int:
        done = 0
        for _ in range(max(limit, 0)):
            runs = self.fsm.claim_due(1)
            if not runs:
                break
            self._run(runs[0])
            done += 1
        return done

    def local_runner(self) -> LocalRunner:
        from .agents import REGISTRY

        return LocalRunner({name: cls({}) for name, cls in REGISTRY.items()})

    # ----------------------------------------------------------------- internals
    def _needs_profile(self, task: sqlite3.Row) -> bool:
        from .agents import NEEDS_PROFILE

        return (task["agent"] or "") in NEEDS_PROFILE

    def _run(self, run: sqlite3.Row) -> None:
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
        if task is None:
            self.fsm.fail(run["id"], "policy", "task_missing")
            return
        if not self.fsm.start(run["id"]):
            return

        lease = None
        if self._needs_profile(task):
            lease = ProfileLease(
                self.cfg.browser.profile_dir,
                wait_s=getattr(self.cfg.core, "profile_wait_s", 30.0),
            )
            try:
                lease.acquire()
            except ProfileBusy as exc:
                self.fsm.fail(run["id"], "transient", str(exc))
                return

        try:
            self._consume(run, task)
        finally:
            if lease is not None:
                lease.release()

    def _consume(self, run: sqlite3.Row, task: sqlite3.Row) -> None:
        intent = {}
        if self.vault is not None:
            try:
                intent = self.vault.open(task["intent_enc"])
            except Exception:  # noqa: BLE001 - undecryptable intent must fail closed
                self.fsm.fail(run["id"], "policy", "intent_unreadable")
                return

        request = {
            "run_id": run["id"],
            "agent": task["agent"] or "noop",
            "task": {
                "id": task["id"],
                "title": task["title"],
                "agent": task["agent"],
                "event_id": task["event_id"],
                "prompt": intent.get("prompt", ""),
                "intent": intent,
            },
            "settings": {
                "allowed_domains": list(domains_for(task["agent"], self.cfg)),
                "sites": {
                    name: dict(spec)
                    for name, spec in (self.cfg.agents.get(task["agent"]).sites
                                       if task["agent"] in self.cfg.agents else {}).items()
                },
                "max_actions_per_min_per_domain": self.cfg.policy.max_actions_per_min_per_domain,
                "artifacts_dir": str(self.artifacts_dir),
                "db_path": self.cfg.core.db_path,
                "browser": {
                    "mode": self.cfg.browser.mode,
                    "cdp_endpoint": self.cfg.browser.cdp_endpoint,
                    "profile_dir": self.cfg.browser.profile_dir,
                },
            },
        }

        inserted = 0
        try:
            for event in self.runner.run(request):
                kind = event.get("event")
                if kind == "steps":
                    for idx, spec in enumerate(event.get("steps", [])):
                        self._insert_step(run["id"], idx, spec)
                        inserted += 1
                elif kind == "step":
                    idx = int(event.get("idx", -1))
                    state = event.get("state")
                    if state == "started":
                        self.fsm.heartbeat(run["id"])
                        self._mark_started(run["id"], idx)
                        if self._step_kind(run["id"], idx) == "publish":
                            payload = json.dumps(
                                {"run_id": run["id"], "task_id": task["id"], "step": idx},
                                sort_keys=True,
                            ).encode()
                            self._artifact(run["id"], "publish_intent", payload)
                    elif state == "done":
                        self._finish_step(run["id"], idx, "done", output_ref=event.get("output_ref"))
                    elif state == "failed":
                        self._finish_step(
                            run["id"], idx, "failed", error_class=event.get("error_class")
                        )
                elif kind == "done":
                    if event.get("ok"):
                        self.fsm.succeed(run["id"])
                    else:
                        self.fsm.fail(
                            run["id"],
                            event.get("error_class") or "transient",
                            event.get("detail", ""),
                        )
                    return
        except RunnerError as exc:
            log.error("runner failed: %s", exc)
            self.fsm.fail(run["id"], "transient", str(exc))
            return

    def _step_kind(self, run_id: str, idx: int) -> str | None:
        row = self.conn.execute(
            "SELECT kind FROM steps WHERE run_id=? AND idx=?", (run_id, idx)
        ).fetchone()
        return row["kind"] if row else None

    def _insert_step(self, run_id: str, idx: int, spec: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO steps(id, run_id, idx, kind, target, state, started_at) "
            "VALUES(?,?,?,?,?,'pending',?)",
            (
                f"stp_{uuid.uuid4().hex[:16]}",
                run_id,
                idx,
                spec.get("kind", "unknown"),
                spec.get("target"),
                iso(self.clock()),
            ),
        )

    def _mark_started(self, run_id: str, idx: int) -> None:
        self.conn.execute(
            "UPDATE steps SET state='started' WHERE run_id=? AND idx=? AND state='pending'",
            (run_id, idx),
        )

    def _finish_step(
        self,
        run_id: str,
        idx: int,
        state: str,
        *,
        output_ref: str | None = None,
        error_class: str | None = None,
    ) -> None:
        if idx < 0:
            return
        self.conn.execute(
            "UPDATE steps SET state=?, output_ref=COALESCE(?, output_ref), error_class=?, ended_at=? "
            "WHERE run_id=? AND idx=?",
            (state, output_ref, error_class, iso(self.clock()), run_id, idx),
        )

    def _artifact(self, run_id: str, kind: str, payload: bytes) -> str:
        directory = self.artifacts_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{kind}.json"
        path.write_bytes(payload)
        self.conn.execute(
            "INSERT INTO artifacts(id, run_id, kind, path, bytes, sha256, created_at) VALUES(?,?,?,?,?,?,?)",
            (
                f"art_{uuid.uuid4().hex[:16]}",
                run_id,
                kind,
                str(path),
                len(payload),
                hashlib.sha256(payload).hexdigest(),
                iso(self.clock()),
            ),
        )
        return str(path)


__all__ = ["Executor", "AgentError"]
