"""Calendar poller: syncToken incremental sync -> commands (design v4.0 §5.1)."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from .calendar_client import AuthExpired, CalendarError, CalendarTransport, RateLimited, SyncTokenInvalid
from .clock import iso, parse_iso
from .config import AppConfig
from .db import tx
from .envelope import b64d, parse_description, verify
from .fsm import Fsm, new_id
from .llm.router import LlmUnavailable
from .policy import Verdict, evaluate_calendar

log = logging.getLogger(__name__)


@dataclass
class PollStats:
    created: int = 0
    skipped_self: int = 0
    cancelled: int = 0
    rejected: int = 0
    resynced: bool = False
    error: str | None = None


class CalendarPoller:
    def __init__(
        self,
        conn: sqlite3.Connection,
        transport: CalendarTransport,
        cfg: AppConfig,
        hmac_secret: bytes,
        fsm: Fsm,
        clock,
        router=None,
    ):
        self.conn = conn
        self.transport = transport
        self.cfg = cfg
        self.secret = hmac_secret
        self.fsm = fsm
        self.clock = clock
        self.router = router

    # ------------------------------------------------------------------ public
    def tick(self) -> PollStats:
        stats = PollStats()
        cursor = self._cursor()
        try:
            try:
                items, next_cursor = self._fetch(cursor)
            except SyncTokenInvalid:
                stats.resynced = True
                items, next_cursor = self._fetch(None)
        except RateLimited as exc:
            stats.error = f"rate_limited:{exc.retry_after:g}"
            self._bump_failures()
            return stats
        except AuthExpired:
            stats.error = "auth_expired"
            self._bump_failures()
            return stats
        except CalendarError as exc:
            stats.error = f"calendar_error:{exc.status}"
            self._bump_failures()
            return stats

        for event in items:
            self._handle(event, stats)
        if next_cursor:
            self._save_cursor(next_cursor, full=cursor is None or stats.resynced)
        self._reset_failures()
        return stats

    # ----------------------------------------------------------------- internals
    def _fetch(self, cursor: str | None) -> tuple[list[dict], str | None]:
        time_min = iso(self.clock() - timedelta(days=7)) if cursor is None else None
        items: list[dict] = []
        page_token: str | None = None
        next_cursor: str | None = None
        while True:
            page = self.transport.list_events(
                self.cfg.core.calendar_id,
                sync_token=cursor,
                page_token=page_token,
                time_min=time_min,
            )
            items.extend(page.get("items", []))
            next_cursor = page.get("nextSyncToken") or next_cursor
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return items, next_cursor

    def _handle(self, event: dict, stats: PollStats) -> None:
        event_id = event.get("id") or ""
        if event.get("status") == "cancelled":
            if self.fsm.cancel_by_event(event_id):
                stats.cancelled += 1
            return
        if self._is_self_write(event):
            stats.skipped_self += 1
            return

        prompt, _log, params_b64 = parse_description(event.get("description", ""))
        signature_ok, reason = False, "no_params"
        envelope: dict = {}
        if params_b64:
            try:
                envelope = json.loads(b64d(params_b64))
                signature_ok, reason = verify(self.secret, envelope, self.clock())
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                signature_ok, reason = False, "bad_params"

        verdict = evaluate_calendar(
            event,
            self.cfg,
            signature_ok=signature_ok,
            signature_reason=reason,
            has_params=bool(params_b64),
        )
        agent = envelope.get("agent")
        confidence = envelope.get("confidence")
        if verdict.allowed and not agent:
            agent, confidence = self._route(prompt)
        if verdict.allowed and not agent:
            verdict = Verdict("reject", "no_agent")
        idem = f"cal:{event_id}:{event.get('updated', 'none')}"
        parsed = {
            "title": (event.get("summary") or "")[:1024],
            "agent": agent,
            "payload_ref": envelope.get("payload_ref"),
            "confidence": confidence,
            "prompt": prompt,
            "due_at": self._due_at(event),
            "calendar_id": self.cfg.core.calendar_id,
            "event_id": event_id,
            "signature_reason": reason,
        }
        with tx(self.conn):
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO commands(
                     id, idem_key, source, actor_id, raw_ref, parsed_json,
                     signature_ok, policy_verdict, received_at, applied_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
                (
                    new_id("cmd"),
                    idem,
                    "calendar",
                    ((event.get("organizer") or {}).get("email") or "")[:200],
                    event_id,
                    json.dumps(parsed, ensure_ascii=False),
                    1 if signature_ok else 0,
                    "allow" if verdict.allowed else f"reject:{verdict.reason}",
                    iso(self.clock()),
                ),
            )
        if cur.rowcount == 1:
            if verdict.allowed:
                stats.created += 1
            else:
                stats.rejected += 1

    def _route(self, prompt: str) -> tuple[str | None, float | None]:
        if self.router is not None:
            try:
                result = self.router.classify(prompt, self.clock())
            except LlmUnavailable:
                result = None
            if result is not None and result.is_task and result.agent:
                return result.agent, result.confidence
        return (self.cfg.core.default_agent or None), None

    def _due_at(self, event: dict) -> str:
        """Normalise to UTC ISO 'Z' so stored timestamps compare lexicographically."""
        start = event.get("start") or {}
        if start.get("dateTime"):
            try:
                return iso(parse_iso(start["dateTime"]))
            except ValueError:
                return start["dateTime"]
        if start.get("date"):
            return f"{start['date']}T00:00:00Z"
        return iso(self.clock())

    def _is_self_write(self, event: dict) -> bool:
        row = self.conn.execute(
            "SELECT updated FROM projected_events WHERE event_id=?", (event.get("id", ""),)
        ).fetchone()
        return row is not None and row["updated"] == event.get("updated")

    def _cursor(self) -> str | None:
        row = self.conn.execute("SELECT cursor FROM projector_state WHERE kind='calendar'").fetchone()
        return row["cursor"] if row else None

    def _save_cursor(self, cursor: str, *, full: bool) -> None:
        now_s = iso(self.clock())
        with tx(self.conn):
            self.conn.execute(
                """INSERT INTO projector_state(kind, cursor, last_full_sync_at, consecutive_failures)
                   VALUES('calendar', ?, ?, 0)
                   ON CONFLICT(kind) DO UPDATE SET
                     cursor=excluded.cursor,
                     last_full_sync_at=COALESCE(excluded.last_full_sync_at, projector_state.last_full_sync_at),
                     consecutive_failures=0""",
                (cursor, now_s if full else None),
            )

    def _bump_failures(self) -> None:
        with tx(self.conn):
            self.conn.execute(
                """INSERT INTO projector_state(kind, cursor, last_full_sync_at, consecutive_failures)
                   VALUES('calendar', NULL, NULL, 1)
                   ON CONFLICT(kind) DO UPDATE SET consecutive_failures=consecutive_failures+1"""
            )

    def _reset_failures(self) -> None:
        with tx(self.conn):
            self.conn.execute(
                "UPDATE projector_state SET consecutive_failures=0 WHERE kind='calendar'"
            )
