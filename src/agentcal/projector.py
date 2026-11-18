"""Calendar projection: state -> colorId + run log, never the source of truth."""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import timedelta

from .calendar_client import CalendarError, CalendarTransport, PreconditionFailed
from .clock import iso
from .config import AppConfig
from .db import tx
from .envelope import merge_description

log = logging.getLogger(__name__)

STATE_COLOR = {
    "pending": "9",      # Blueberry
    "claimed": "9",
    "running": "5",      # Banana
    "blocked_human": "6",  # Tangerine
    "success": "10",     # Basil
    "failed": "11",      # Tomato
    "canceled": "8",     # Graphite
}
MAX_PROJECTION_ATTEMPTS = 3
RETRY_BASE_S = 60


class CalendarProjector:
    def __init__(self, conn: sqlite3.Connection, transport: CalendarTransport | None, cfg: AppConfig, clock):
        self.conn = conn
        self.transport = transport
        self.cfg = cfg
        self.clock = clock

    def flush(self, limit: int = 20) -> int:
        now_s = iso(self.clock())
        rows = self.conn.execute(
            "SELECT * FROM projection_outbox WHERE next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (now_s, limit),
        ).fetchall()
        done = 0
        for row in rows:
            if self.project_task(row["task_id"]):
                with tx(self.conn):
                    self.conn.execute(
                        "DELETE FROM projection_outbox WHERE task_id=? AND kind=?",
                        (row["task_id"], row["kind"]),
                    )
                done += 1
            else:
                self._defer(row)
        return done

    def project_task(self, task_id: str) -> bool:
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return True
        if self.transport is None or not task["event_id"] or not task["calendar_id"]:
            return True
        try:
            log_text = self._log_lines(task)
        except Exception:  # noqa: BLE001 - projection must never break the FSM
            log_text = f"state: {task['state']}"

        color = STATE_COLOR.get(task["state"], "9")
        last_error = "conflict"
        for _ in range(MAX_PROJECTION_ATTEMPTS):
            try:
                event = self.transport.get_event(task["calendar_id"], task["event_id"])
            except CalendarError as exc:
                self._remember_error(task_id, str(exc))
                return False
            merged = merge_description(event.get("description") or "", log=log_text)
            try:
                result = self.transport.patch_event(
                    task["calendar_id"],
                    task["event_id"],
                    {"description": merged, "colorId": color},
                    etag=event.get("etag") or "",
                )
            except PreconditionFailed as exc:
                last_error = str(exc)
                continue
            except CalendarError as exc:
                self._remember_error(task_id, str(exc))
                return False
            with tx(self.conn):
                self.conn.execute(
                    """INSERT INTO projected_events(event_id, updated, projected_at) VALUES(?,?,?)
                       ON CONFLICT(event_id) DO UPDATE SET updated=excluded.updated, projected_at=excluded.projected_at""",
                    (task["event_id"], result.get("updated", ""), iso(self.clock())),
                )
            return True
        self._remember_error(task_id, last_error)
        return False

    # ----------------------------------------------------------------- internals
    def _log_lines(self, task: sqlite3.Row) -> str:
        run = self.conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1", (task["id"],)
        ).fetchone()
        steps = []
        if run is not None:
            steps = self.conn.execute(
                "SELECT * FROM steps WHERE run_id=? ORDER BY idx", (run["id"],)
            ).fetchall()
        done = sum(1 for s in steps if s["state"] == "done")
        total = len(steps)
        current = steps[-1] if steps else None
        step_txt = "-"
        if current is not None:
            target = f" → {current['target']}" if current["target"] else ""
            step_txt = f"{done}/{total} {current['kind']}{target}"
        artifacts = self.conn.execute(
            "SELECT COUNT(*) AS n FROM artifacts WHERE run_id=?", (run["id"] if run else "",)
        ).fetchone()["n"]
        return "\n".join(
            [
                f"state: {task['state']} | attempt: {task['attempt']} | started: {(run['started_at'] if run else '-') or '-'}",
                f"step: {step_txt}",
                f"last_error: {(run['exit_reason'] if run and run['exit_reason'] else '-')}",
                f"artifacts: {artifacts} files",
                f"updated: {iso(self.clock())}",
            ]
        )

    def _remember_error(self, task_id: str, message: str) -> None:
        with tx(self.conn):
            self.conn.execute(
                "UPDATE projection_outbox SET last_error=? WHERE task_id=?", (message[:500], task_id)
            )

    def _defer(self, row: sqlite3.Row) -> None:
        attempts = row["attempts"] + 1
        delay = min(RETRY_BASE_S * (2 ** min(attempts, 6)), 3600)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE projection_outbox SET attempts=?, next_attempt_at=? WHERE task_id=? AND kind=?",
                (
                    attempts,
                    iso(self.clock() + timedelta(seconds=delay)),
                    row["task_id"],
                    row["kind"],
                ),
            )
