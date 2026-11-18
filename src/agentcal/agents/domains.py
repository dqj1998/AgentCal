"""Shared domain gate for browser agents: config seeds plus permanent approvals."""
from __future__ import annotations

from urllib.parse import urlparse

from ..runner.protocol import AgentError


def check_domain(url: str, settings: dict) -> None:
    allowed = settings.get("allowed_domains")
    if allowed is None:
        return
    from ..policy import host_allowed, host_in, trusted_domains

    host = urlparse(url).hostname or ""
    if host_allowed(url, allowed):
        return
    db_path = settings.get("db_path")
    if db_path:
        from ..db import connect

        conn = connect(db_path)
        try:
            if host_in(host, trusted_domains(conn)):
                return
        finally:
            conn.close()
    raise AgentError("human_required", f"new_domain:{host}")
