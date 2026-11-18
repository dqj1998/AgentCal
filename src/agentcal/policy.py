"""Authorization gate (design v4.0 §7). Fail closed."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlparse

from .clock import iso
from .config import AppConfig

HIGH_RISK = {"payment", "delete_account", "change_permission", "bulk_publish"}
MEDIUM_RISK = {"publish", "submit", "delete_single", "send_message"}


@dataclass(frozen=True)
class Verdict:
    decision: str  # "allow" | "reject" | "ask"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


@dataclass(frozen=True)
class ActionVerdict:
    decision: str  # "allow" | "require_approval" | "reject"
    reason: str
    risk: str


def evaluate_calendar(
    event: dict,
    cfg: AppConfig,
    *,
    signature_ok: bool,
    signature_reason: str = "unsigned",
    has_params: bool = False,
) -> Verdict:
    """Human-authored events carry no PARAMS; system parameters must be signed.

    - PARAMS present  -> signature is mandatory (a modified payload is rejected).
    - PARAMS absent   -> treat as human intent: only the calendar owner may submit it.
    """
    if has_params and not signature_ok:
        return Verdict("reject", signature_reason)
    owner = (cfg.core.owner_email or "").strip().lower()
    if not owner:
        return Verdict("reject", "no_owner_configured")
    organizer = ((event.get("organizer") or {}).get("email") or "").strip().lower()
    creator = ((event.get("creator") or {}).get("email") or "").strip().lower()
    calendar_id = (cfg.core.calendar_id or "").strip().lower()
    # On a secondary calendar Google sets organizer to the calendar id, not to the user,
    # so accept that shape only when the creator is the owner as well.
    trusted = organizer == owner or (bool(calendar_id) and organizer == calendar_id and creator == owner)
    if not trusted:
        return Verdict("reject", "not_owner")
    if event.get("status") == "cancelled":
        return Verdict("reject", "cancelled")
    return Verdict("allow", "signed" if has_params else "human_intent")


def domains_for(agent: str | None, cfg: AppConfig) -> tuple[str, ...]:
    """Per-agent allowlist wins; agents without a section inherit the global one."""
    if agent and agent in cfg.agents:
        return cfg.agents[agent].domains
    return cfg.policy.allowed_domains


def host_allowed(url: str, domains) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def domain_allowed(url: str, cfg: AppConfig, agent: str | None = None) -> bool:
    return host_allowed(url, domains_for(agent, cfg))


def trusted_domains(conn: sqlite3.Connection | None) -> list[str]:
    if conn is None:
        return []
    return [row["domain"] for row in conn.execute("SELECT domain FROM trusted_domains ORDER BY domain")]


def trust_domain(conn: sqlite3.Connection, domain: str, source: str, now_s: str, note: str | None = None) -> bool:
    domain = (domain or "").strip().lower().lstrip(".")
    if not domain or "/" in domain or " " in domain:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO trusted_domains(domain, source, created_at, note) VALUES(?,?,?,?)",
        (domain, source, now_s, note),
    )
    return cur.rowcount == 1


def revoke_domain(conn: sqlite3.Connection, domain: str) -> bool:
    cur = conn.execute("DELETE FROM trusted_domains WHERE domain=?", ((domain or "").strip().lower(),))
    return cur.rowcount == 1


def host_in(host: str, domains) -> bool:
    host = (host or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in domains)


def risk_of(action: str) -> str:
    if action in HIGH_RISK:
        return "high"
    if action in MEDIUM_RISK:
        return "medium"
    return "low"


def evaluate_action(action: str, target_url: str, cfg: AppConfig) -> ActionVerdict:
    risk = risk_of(action)
    if not domain_allowed(target_url, cfg):
        return ActionVerdict("reject", "domain_not_allowed", risk)
    if risk == "high":
        return ActionVerdict("require_approval", "high_risk_action", risk)
    return ActionVerdict("allow", "ok", risk)


class RateGate:
    """Sliding 60s window per domain, persisted so restarts cannot reset the budget."""

    def __init__(self, conn: sqlite3.Connection, clock):
        self.conn = conn
        self.clock = clock

    def allow(self, domain: str, limit_per_min: int) -> bool:
        now = self.clock()
        cutoff = iso(now - timedelta(seconds=60))
        self.conn.execute("DELETE FROM rate_events WHERE at < ?", (cutoff,))
        used = self.conn.execute(
            "SELECT COUNT(*) AS n FROM rate_events WHERE domain=?", (domain,)
        ).fetchone()["n"]
        if used >= max(limit_per_min, 1):
            return False
        self.conn.execute(
            "INSERT INTO rate_events(id, domain, at) VALUES(?,?,?)",
            (uuid.uuid4().hex[:16], domain, iso(now)),
        )
        return True
