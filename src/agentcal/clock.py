"""Time helpers: all persisted timestamps are UTC ISO-8601 seconds."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def plus(seconds: float) -> str:
    return iso(utcnow() + timedelta(seconds=seconds))
