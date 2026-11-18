"""Command -> task materialisation. Idempotent on idem_key."""
from __future__ import annotations

import json
import logging
import sqlite3

from .clock import iso
from .config import AppConfig
from .db import tx
from .fsm import Fsm, new_id

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, conn: sqlite3.Connection, cfg: AppConfig, fsm: Fsm, clock, vault):
        self.conn = conn
        self.cfg = cfg
        self.fsm = fsm
        self.clock = clock
        self.vault = vault

    def apply_commands(self, limit: int = 50) -> int:
        rows = self.conn.execute(
            "SELECT * FROM commands WHERE applied_at IS NULL ORDER BY received_at LIMIT ?", (limit,)
        ).fetchall()
        applied = 0
        for row in rows:
            now_s = iso(self.clock())
            with tx(self.conn):
                fresh = self.conn.execute(
                    "SELECT applied_at FROM commands WHERE id=?", (row["id"],)
                ).fetchone()
                if fresh is None or fresh["applied_at"]:
                    continue
                if row["policy_verdict"] == "allow":
                    parsed = json.loads(row["parsed_json"] or "{}")
                    due_at = parsed.get("due_at") or now_s
                    intent = {
                        "agent": parsed.get("agent"),
                        "payload_ref": parsed.get("payload_ref"),
                        "prompt": parsed.get("prompt", ""),
                        "event_id": parsed.get("event_id"),
                    }
                    self.conn.execute(
                        """INSERT OR IGNORE INTO tasks(
                             id, idem_key, source, actor_id, title, intent_enc, agent, confidence,
                             state, attempt, not_before, due_at, policy_snap,
                             calendar_id, event_id, created_at, updated_at
                           ) VALUES(?,?,?,?,?,?,?,?,'pending',0,?,?,?,?,?,?,?)""",
                        (
                            new_id("tsk"),
                            row["idem_key"],
                            row["source"],
                            row["actor_id"],
                            parsed.get("title", "")[:1024],
                            self.vault.seal(intent),
                            parsed.get("agent"),
                            parsed.get("confidence"),
                            due_at,
                            due_at,
                            json.dumps({"decision": "allow"}),
                            parsed.get("calendar_id"),
                            parsed.get("event_id"),
                            now_s,
                            now_s,
                        ),
                    )
                    applied += 1
                self.conn.execute("UPDATE commands SET applied_at=? WHERE id=?", (now_s, row["id"]))
        return applied
