"""Task/run state machine: the only writer of task state (design v4.0 §4)."""
from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .clock import iso, parse_iso, utcnow
from .config import AppConfig
from .db import tx

log = logging.getLogger(__name__)

TERMINAL_TASK_STATES = frozenset({"success", "failed", "canceled"})
BACKOFF_S = (30, 120, 480)
APPROVAL_TTL_S = 86400


@dataclass
class ReconcileReport:
    orphaned: int = 0
    retried: int = 0
    blocked: int = 0
    missed: int = 0
    approvals_expired: int = 0


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


class Fsm:
    def __init__(self, conn: sqlite3.Connection, cfg: AppConfig, clock: Callable[[], datetime] = utcnow):
        self.conn = conn
        self.cfg = cfg
        self.clock = clock

    # ---------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return self.clock()

    def _lease(self, now: datetime) -> str:
        return iso(now + timedelta(seconds=self.cfg.core.lease_ttl_s))

    def _backoff(self, attempt: int) -> timedelta:
        idx = min(max(attempt - 1, 0), len(BACKOFF_S) - 1)
        return timedelta(seconds=BACKOFF_S[idx])

    def _enqueue_notification(self, task_id: str, kind: str, now_s: str) -> None:
        self.conn.execute(
            "INSERT INTO notifications(id, task_id, kind, attempts, next_attempt_at, last_error) "
            "VALUES(?,?,?,0,?,NULL)",
            (new_id("ntf"), task_id, kind, now_s),
        )

    def _enqueue_projection(self, task_id: str, now_s: str) -> None:
        self.conn.execute(
            """INSERT INTO projection_outbox(task_id, kind, attempts, next_attempt_at, last_error)
               VALUES(?, 'calendar', 0, ?, NULL)
               ON CONFLICT(task_id, kind) DO UPDATE SET next_attempt_at=excluded.next_attempt_at""",
            (task_id, now_s),
        )

    def task(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()

    # ------------------------------------------------------------- transitions
    def claim_due(self, limit: int = 10) -> list[sqlite3.Row]:
        now = self._now()
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE state='pending' AND not_before<=? ORDER BY not_before LIMIT ?",
            (iso(now), limit),
        ).fetchall()
        runs = []
        for row in rows:
            run = self.claim(row["id"])
            if run is not None:
                runs.append(run)
        return runs

    def claim(self, task_id: str, lease_owner: str = "supervisor") -> sqlite3.Row | None:
        now = self._now()
        now_s = iso(now)
        run_id = new_id("run")
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE tasks SET state='claimed', attempt=attempt+1, updated_at=? "
                "WHERE id=? AND state='pending' AND not_before<=?",
                (now_s, task_id, now_s),
            )
            if cur.rowcount != 1:
                return None
            attempt = self.conn.execute(
                "SELECT attempt FROM tasks WHERE id=?", (task_id,)
            ).fetchone()["attempt"]
            self.conn.execute(
                "INSERT INTO runs(id, task_id, attempt_no, state, lease_owner, lease_expires_at) "
                "VALUES(?,?,?,'leased',?,?)",
                (run_id, task_id, attempt, lease_owner, self._lease(now)),
            )
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def start(self, run_id: str) -> bool:
        """leased -> running, or blocked -> running (resume)."""
        now = self._now()
        now_s = iso(now)
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE runs SET state='running', lease_owner='supervisor', lease_expires_at=?, "
                "started_at=COALESCE(started_at, ?) WHERE id=? AND state IN ('leased','blocked')",
                (self._lease(now), now_s, run_id),
            )
            if cur.rowcount != 1:
                return False
            self.conn.execute(
                "UPDATE tasks SET state='running', updated_at=? WHERE id=(SELECT task_id FROM runs WHERE id=?)",
                (now_s, run_id),
            )
        return True

    def heartbeat(self, run_id: str) -> None:
        with tx(self.conn):
            self.conn.execute(
                "UPDATE runs SET lease_expires_at=? WHERE id=? AND state IN ('leased','running')",
                (self._lease(self._now()), run_id),
            )

    def succeed(self, run_id: str) -> bool:
        now_s = iso(self._now())
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE runs SET state='succeeded', ended_at=?, exit_reason=NULL "
                "WHERE id=? AND state='running'",
                (now_s, run_id),
            )
            if cur.rowcount != 1:
                return False
            task_id = self.conn.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()["task_id"]
            self.conn.execute("UPDATE tasks SET state='success', updated_at=? WHERE id=?", (now_s, task_id))
            self._enqueue_projection(task_id, now_s)
            self._enqueue_notification(task_id, "success", now_s)
        return True

    def fail(self, run_id: str, error_class: str, detail: str = "") -> bool:
        now = self._now()
        now_s = iso(now)
        with tx(self.conn):
            cur = self.conn.execute(
                "UPDATE runs SET state='failed', ended_at=?, exit_reason=? "
                "WHERE id=? AND state IN ('leased','running','blocked')",
                (now_s, f"{error_class}:{detail}"[:500], run_id),
            )
            if cur.rowcount != 1:
                return False
            row = self.conn.execute(
                "SELECT t.id AS task_id, t.attempt FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.id=?",
                (run_id,),
            ).fetchone()
            task_id, attempt = row["task_id"], row["attempt"]
            if error_class == "transient" and attempt < self.cfg.core.max_attempts:
                self.conn.execute(
                    "UPDATE tasks SET state='pending', not_before=?, updated_at=? WHERE id=?",
                    (iso(now + self._backoff(attempt)), now_s, task_id),
                )
            elif error_class in ("human_required", "unknown_side_effect"):
                self._block_task(task_id, run_id, detail or error_class, now_s)
            else:
                self.conn.execute("UPDATE tasks SET state='failed', updated_at=? WHERE id=?", (now_s, task_id))
                self._enqueue_notification(task_id, "failed", now_s)
            self._enqueue_projection(task_id, now_s)
        return True

    def block(self, run_id: str, reason: str) -> bool:
        now_s = iso(self._now())
        with tx(self.conn):
            row = self.conn.execute("SELECT task_id FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return False
            self._block_task(row["task_id"], run_id, reason, now_s)
            self._enqueue_projection(row["task_id"], now_s)
        return True

    def _block_task(self, task_id: str, run_id: str | None, reason: str, now_s: str) -> None:
        self.conn.execute("UPDATE tasks SET state='blocked_human', updated_at=? WHERE id=?", (now_s, task_id))
        if run_id:
            self.conn.execute(
                "UPDATE runs SET state='blocked', lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                (run_id,),
            )
        self._enqueue_notification(task_id, "blocked", now_s)
        pending = self.conn.execute(
            "SELECT 1 FROM approvals WHERE task_id=? AND decision IS NULL", (task_id,)
        ).fetchone()
        if pending is None:
            expires = iso(parse_iso(now_s) + timedelta(seconds=APPROVAL_TTL_S))
            self.conn.execute(
                "INSERT INTO approvals(id, task_id, run_id, reason, requested_at, expires_at, decision) "
                "VALUES(?,?,?,?,?,?,NULL)",
                (new_id("ap"), task_id, run_id, reason, now_s, expires),
            )

    def resolve_approval(self, task_id: str, decision: str, actor: str, note: str = "") -> bool:
        if decision not in ("approve", "deny"):
            raise ValueError("decision must be approve|deny")
        now = self._now()
        now_s = iso(now)
        with tx(self.conn):
            ap = self.conn.execute(
                "SELECT * FROM approvals WHERE task_id=? AND decision IS NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if ap is None:
                return False
            self.conn.execute(
                "UPDATE approvals SET decision=?, decided_by=?, decided_at=? WHERE id=?",
                (decision, actor, now_s, ap["id"]),
            )
            reason = (ap["reason"] or "")
            if decision == "approve" and reason.startswith("new_domain:"):
                domain = reason.split(":", 1)[1].strip().lower()
                if domain:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO trusted_domains(domain, source, created_at) VALUES(?, 'approval', ?)",
                        (domain, now_s),
                    )
            if decision == "approve":
                if ap["run_id"]:
                    cur = self.conn.execute(
                        "UPDATE runs SET state='running', lease_owner='supervisor', lease_expires_at=? "
                        "WHERE id=? AND state='blocked'",
                        (self._lease(now), ap["run_id"]),
                    )
                    if cur.rowcount == 1:
                        self.conn.execute(
                            "UPDATE tasks SET state='running', updated_at=? WHERE id=?", (now_s, task_id)
                        )
                    else:
                        self.conn.execute(
                            "UPDATE tasks SET state='pending', not_before=?, updated_at=? WHERE id=?",
                            (now_s, now_s, task_id),
                        )
                else:
                    self.conn.execute(
                        "UPDATE tasks SET state='pending', not_before=?, updated_at=? WHERE id=?",
                        (now_s, now_s, task_id),
                    )
            else:
                if ap["run_id"]:
                    self.conn.execute(
                        "UPDATE runs SET state='failed', ended_at=?, exit_reason='denied' "
                        "WHERE id=? AND state IN ('blocked','running','leased')",
                        (now_s, ap["run_id"]),
                    )
                self.conn.execute("UPDATE tasks SET state='canceled', updated_at=? WHERE id=?", (now_s, task_id))
                self._enqueue_notification(task_id, "canceled", now_s)
            self._enqueue_projection(task_id, now_s)
        return True

    def cancel(self, task_id: str, actor: str = "human", reason: str = "canceled") -> bool:
        now_s = iso(self._now())
        with tx(self.conn):
            row = self.conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in TERMINAL_TASK_STATES:
                return False
            self.conn.execute("UPDATE tasks SET state='canceled', updated_at=? WHERE id=?", (now_s, task_id))
            self.conn.execute(
                "UPDATE runs SET state='failed', ended_at=?, exit_reason=? "
                "WHERE task_id=? AND state IN ('leased','running','blocked')",
                (now_s, reason, task_id),
            )
            self.conn.execute(
                "UPDATE approvals SET decision='canceled', decided_by=?, decided_at=? "
                "WHERE task_id=? AND decision IS NULL",
                (actor, now_s, task_id),
            )
            self._enqueue_projection(task_id, now_s)
            self._enqueue_notification(task_id, "canceled", now_s)
        return True

    def retry(self, task_id: str, actor: str = "human") -> bool:
        """Human override: failed/canceled/blocked -> pending with a fresh attempt budget."""
        now_s = iso(self._now())
        with tx(self.conn):
            row = self.conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] not in ("failed", "canceled", "blocked_human"):
                return False
            self.conn.execute(
                "UPDATE approvals SET decision='canceled', decided_by=?, decided_at=? "
                "WHERE task_id=? AND decision IS NULL",
                (actor, now_s, task_id),
            )
            self.conn.execute(
                "UPDATE tasks SET state='pending', attempt=0, not_before=?, updated_at=? WHERE id=?",
                (now_s, now_s, task_id),
            )
            self._enqueue_projection(task_id, now_s)
        return True

    def cancel_by_event(self, event_id: str, reason: str = "calendar_cancelled") -> bool:
        row = self.conn.execute(
            "SELECT id FROM tasks WHERE event_id=? AND state NOT IN ('success','failed','canceled')",
            (event_id,),
        ).fetchone()
        if row is None:
            return False
        return self.cancel(row["id"], actor="calendar", reason=reason)

    # ------------------------------------------------------------- reconciliation
    def reconcile(self) -> ReconcileReport:
        now = self._now()
        now_s = iso(now)
        report = ReconcileReport()
        with tx(self.conn):
            expired = self.conn.execute(
                "SELECT * FROM approvals WHERE decision IS NULL AND expires_at<=?", (now_s,)
            ).fetchall()
            for ap in expired:
                self.conn.execute(
                    "UPDATE approvals SET decision='timeout', decided_at=? WHERE id=?", (now_s, ap["id"])
                )
                if ap["run_id"]:
                    self.conn.execute(
                        "UPDATE runs SET state='failed', ended_at=?, exit_reason='approval_timeout' "
                        "WHERE id=? AND state IN ('blocked','running','leased')",
                        (now_s, ap["run_id"]),
                    )
                self.conn.execute(
                    "UPDATE tasks SET state='failed', updated_at=? WHERE id=? AND state='blocked_human'",
                    (now_s, ap["task_id"]),
                )
                self._enqueue_projection(ap["task_id"], now_s)
                report.approvals_expired += 1

            orphans = self.conn.execute(
                "SELECT r.id AS run_id, r.task_id, t.attempt FROM runs r JOIN tasks t ON t.id=r.task_id "
                "WHERE r.state IN ('leased','running') AND r.lease_expires_at IS NOT NULL "
                "AND r.lease_expires_at<=?",
                (now_s,),
            ).fetchall()
            for row in orphans:
                self.conn.execute(
                    "UPDATE runs SET state='orphaned', ended_at=?, exit_reason='lease_expired' WHERE id=?",
                    (now_s, row["run_id"]),
                )
                report.orphaned += 1
                # 只有真正“开始过”的 publish 才意味着副作用未知；未开始的 publish 行可安全重试
                side_effect = self.conn.execute(
                    "SELECT 1 FROM steps WHERE run_id=? AND kind='publish' "
                    "AND state IN ('started','done','failed')",
                    (row["run_id"],),
                ).fetchone()
                if side_effect is not None:
                    self._block_task(row["task_id"], None, "unknown_side_effect", now_s)
                    report.blocked += 1
                elif row["attempt"] >= self.cfg.core.max_attempts:
                    self.conn.execute(
                        "UPDATE tasks SET state='failed', updated_at=? WHERE id=?", (now_s, row["task_id"])
                    )
                else:
                    self.conn.execute(
                        "UPDATE tasks SET state='pending', not_before=?, updated_at=? WHERE id=?",
                        (now_s, now_s, row["task_id"]),
                    )
                    report.retried += 1
                self._enqueue_projection(row["task_id"], now_s)

            missed = self.conn.execute(
                "SELECT id FROM tasks WHERE state='pending' AND attempt=0 AND due_at<=?",
                (iso(now - timedelta(seconds=self.cfg.core.grace_s)),),
            ).fetchall()
            for row in missed:
                if self.cfg.core.missed_policy == "ask":
                    self._block_task(row["id"], None, "missed", now_s)
                    report.blocked += 1
                else:
                    self.conn.execute(
                        "UPDATE tasks SET not_before=?, updated_at=? WHERE id=?", (now_s, now_s, row["id"])
                    )
                    report.missed += 1
        return report


__all__ = ["Fsm", "ReconcileReport", "TERMINAL_TASK_STATES", "new_id"]
