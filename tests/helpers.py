"""Shared fixtures: fake clock, fake calendar, task builders."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from agentcal.calendar_client import CalendarError, PreconditionFailed, RateLimited, SyncTokenInvalid
from agentcal.clock import UTC, iso
from agentcal.config import AppConfig, CoreConfig
from agentcal.db import connect, migrate
from agentcal.envelope import b64e, render_description, sign

SECRET = b"h" * 32
VAULT_KEY = b"v" * 32
OWNER = "me@example.com"
CAL_ID = "agentcal@example.com"


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t = self.t + timedelta(seconds=seconds)


def make_config(tmp_path: Path, **core_overrides) -> AppConfig:
    core = CoreConfig(
        calendar_id=CAL_ID,
        owner_email=OWNER,
        db_path=str(tmp_path / "agentcal.db"),
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    if core_overrides:
        core = replace(core, **core_overrides)
    return AppConfig(core=core)


def make_env(tmp_path: Path, **core_overrides):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = make_config(tmp_path, **core_overrides)
    conn = connect(cfg.core.db_path)
    migrate(conn)
    return cfg, conn


def envelope(agent: str = "noop", tid: str = "tsk_test", exp: str = "2026-12-01T00:00:00Z", tamper: bool = False) -> dict:
    env = {"v": 1, "tid": tid, "nonce": "n1", "exp": exp, "agent": agent, "payload_ref": f"vault:{tid}"}
    signed = sign(SECRET, env)
    if tamper:
        signed["agent"] = "evil-agent"
    return signed


def description(prompt: str = "publish weekly note", *, env: dict | None = None, log: str = "state: pending") -> str:
    params = b64e(json.dumps(env or envelope()).encode())
    return render_description(prompt, log, params)


def make_event(
    event_id: str = "evt1",
    *,
    desc: str | None = None,
    summary: str = "weekly note",
    start: str = "2026-01-01T08:00:00Z",
    organizer: str = OWNER,
    status: str = "confirmed",
) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "description": desc if desc is not None else description(),
        "start": {"dateTime": start},
        "organizer": {"email": organizer},
        "status": status,
    }


class FakeCalendar:
    """In-memory Calendar transport: syncToken, etag/412, 410, 429."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.events: dict[str, dict] = {}
        self.version = 0
        self.patch_conflicts_remaining = 0
        self.invalid_sync_once = False
        self.rate_limit_once = False
        self.calls = {"list": 0, "get": 0, "patch": 0}

    def _stamp(self, event: dict) -> dict:
        self.version += 1
        event["_v"] = self.version
        event["updated"] = f"2026-01-01T00:00:{self.version % 60:02d}Z"
        event["etag"] = f"etag-{self.version}"
        return event

    def add(self, event: dict) -> dict:
        self.events[event["id"]] = self._stamp(dict(event))
        return self.events[event["id"]]

    def cancel(self, event_id: str) -> None:
        event = self.events[event_id]
        event["status"] = "cancelled"
        self._stamp(event)

    def list_events(self, calendar_id: str, *, sync_token=None, page_token=None, time_min=None,
                    single_events=True, show_deleted=True, max_results=250) -> dict:
        self.calls["list"] += 1
        if self.rate_limit_once:
            self.rate_limit_once = False
            raise RateLimited(1.0)
        if sync_token is None:
            items = [dict(event) for event in self.events.values()]
        else:
            if self.invalid_sync_once:
                self.invalid_sync_once = False
                raise SyncTokenInvalid("gone", 410)
            since = int(sync_token)
            items = [dict(event) for event in self.events.values() if event["_v"] > since]
        return {"items": items, "nextSyncToken": str(self.version)}

    def get_event(self, calendar_id: str, event_id: str) -> dict:
        self.calls["get"] += 1
        if event_id not in self.events:
            raise CalendarError("not found", 404)
        return dict(self.events[event_id])

    def patch_event(self, calendar_id: str, event_id: str, body: dict, etag: str) -> dict:
        self.calls["patch"] += 1
        if self.patch_conflicts_remaining > 0:
            self.patch_conflicts_remaining -= 1
            raise PreconditionFailed("etag mismatch", 412)
        event = self.events[event_id]
        if etag and etag != event["etag"]:
            raise PreconditionFailed("etag mismatch", 412)
        event.update({k: v for k, v in body.items() if k in ("description", "colorId")})
        return dict(self._stamp(event))


def insert_task(
    conn,
    cfg: AppConfig,
    *,
    task_id: str = "tsk_manual",
    event_id: str | None = "evt1",
    state: str = "pending",
    attempt: int = 0,
    due: str = "2026-01-01T08:00:00Z",
    not_before: str | None = None,
    agent: str = "noop",
    calendar_id: str | None = None,
    intent: dict | None = None,
) -> str:
    from agentcal.vault import Vault
    now_s = iso(datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC))
    conn.execute(
        """INSERT INTO tasks(id, idem_key, source, actor_id, title, intent_enc, agent, confidence,
             state, attempt, not_before, due_at, policy_snap, calendar_id, event_id, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            task_id,
            f"test:{task_id}",
            "calendar",
            OWNER,
            "weekly note",
            Vault(VAULT_KEY).seal(intent or {"prompt": "weekly note"}),
            agent,
            None,
            state,
            attempt,
            not_before or due,
            due,
            json.dumps({"decision": "allow"}),
            calendar_id if calendar_id is not None else cfg.core.calendar_id,
            event_id,
            now_s,
            now_s,
        ),
    )
    return task_id


def enqueue_projection(conn, task_id: str, at: str = "2026-01-01T09:00:00Z") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO projection_outbox(task_id, kind, attempts, next_attempt_at, last_error) "
        "VALUES(?, 'calendar', 0, ?, NULL)",
        (task_id, at),
    )
