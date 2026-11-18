"""Outbound notifications: task state -> Telegram card (never blocks the FSM)."""
from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta

from .clock import iso
from .config import AppConfig
from .db import tx
from .telegram import ChatRateLimiter, TelegramClient, TelegramError, callback_data, short_id

log = logging.getLogger(__name__)

ICONS = {"success": "✅", "failed": "❌", "blocked": "⏳", "canceled": "⚪"}


class TelegramNotifier:
    def __init__(self, conn: sqlite3.Connection, client: TelegramClient | None, cfg: AppConfig, clock):
        self.conn = conn
        self.client = client
        self.cfg = cfg
        self.clock = clock
        self.limiter = ChatRateLimiter(clock)

    def system(self, text: str, key: str = "system", throttle_s: int = 3600) -> bool:
        """Throttled operator alert (auth expiry, degraded mode)."""
        if self.client is None:
            return False
        chat = next(iter(self.cfg.policy.allowed_chats), None)
        if chat is None:
            return False
        now = int(self.clock().timestamp())
        row = self.conn.execute("SELECT offset FROM tg_state WHERE kind=?", ("sys:" + key,)).fetchone()
        if row is not None and now - int(row["offset"]) < throttle_s:
            return False
        try:
            self.client.send_message(chat, text)
        except TelegramError as exc:
            log.warning("system alert failed: %s", exc)
            return False
        with tx(self.conn):
            self.conn.execute(
                "INSERT INTO tg_state(kind, offset) VALUES(?,?)"
                " ON CONFLICT(kind) DO UPDATE SET offset=excluded.offset",
                ("sys:" + key, now),
            )
        return True

    def flush(self, limit: int = 10) -> int:
        if self.client is None:
            with tx(self.conn):
                self.conn.execute(
                    "DELETE FROM notifications WHERE next_attempt_at < ?",
                    (iso(self.clock() - timedelta(days=30)),),
                )
            return 0
        now_s = iso(self.clock())
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?",
            (now_s, limit),
        ).fetchall()
        sent = 0
        for row in rows:
            if self._send(row):
                with tx(self.conn):
                    self.conn.execute("DELETE FROM notifications WHERE id=?", (row["id"],))
                sent += 1
        return sent

    # ----------------------------------------------------------------- internals
    def _send(self, row: sqlite3.Row) -> bool:
        chat_id = next(iter(self.cfg.policy.allowed_chats), None)
        if chat_id is None:
            self._defer(row, "no_chat_configured")
            return False
        if not self.limiter.ok(chat_id):
            self._defer(row, "rate_limited")
            return False
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        if task is None:
            return True  # nothing to say; drop
        text, keyboard = self._render(task, row["kind"])
        try:
            result = self.client.send_message(chat_id, text, keyboard=keyboard)
        except TelegramError as exc:
            self._defer(row, str(exc))
            return False
        message_id = (result or {}).get("message_id")
        if message_id:
            with tx(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO tg_messages(chat_id, message_id, thread_id, task_id, kind, updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (str(chat_id), int(message_id), None, task["id"], row["kind"], iso(self.clock())),
                )
        return True

    def _render(self, task: sqlite3.Row, kind: str) -> tuple[str, dict | None]:
        icon = ICONS.get(kind, "•")
        lines = [f"{icon} {task['title'][:200]}", f"id: {task['id']}", f"state: {task['state']}"]
        keyboard = None
        if kind == "blocked":
            approval = self.conn.execute(
                "SELECT reason FROM approvals WHERE task_id=? AND decision IS NULL "
                "ORDER BY requested_at DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
            if approval:
                lines.append(f"reason: {approval['reason']}")
            sid = short_id(task["id"])
            with tx(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO tg_callbacks(short_id, task_id, created_at) VALUES(?,?,?)",
                    (sid, task["id"], iso(self.clock())),
                )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "批准", "callback_data": callback_data("q", task["id"], "a")},
                        {"text": "拒绝", "callback_data": callback_data("q", task["id"], "d")},
                    ]
                ]
            }
        elif kind == "failed":
            sid = short_id(task["id"])
            with tx(self.conn):
                self.conn.execute(
                    "INSERT OR REPLACE INTO tg_callbacks(short_id, task_id, created_at) VALUES(?,?,?)",
                    (sid, task["id"], iso(self.clock())),
                )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "重试", "callback_data": callback_data("q", task["id"], "r")}]
                ]
            }
        return "\n".join(lines), keyboard

    def _defer(self, row: sqlite3.Row, reason: str) -> None:
        attempts = row["attempts"] + 1
        delay = min(30 * (2 ** min(attempts, 6)), 1800)
        with tx(self.conn):
            self.conn.execute(
                "UPDATE notifications SET attempts=?, next_attempt_at=?, last_error=? WHERE id=?",
                (attempts, iso(self.clock() + timedelta(seconds=delay)), reason[:500], row["id"]),
            )


__all__ = ["TelegramNotifier"]
