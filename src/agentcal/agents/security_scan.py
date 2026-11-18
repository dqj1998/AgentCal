"""Internal security scan: fetch allow-listed URLs, record findings, never mutate the target."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..clock import utcnow
from ..db import connect
from ..policy import RateGate
from ..runner.protocol import Agent, AgentError, Step

URL_RE = re.compile(r'https?://[^\s"<>)\]}]+')


def extract_urls(text: str) -> list[str]:
    return list(dict.fromkeys(URL_RE.findall(text or "")))


class SecurityScanAgent(Agent):
    name = "security-scan"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.allowed = [d.lower() for d in self.settings.get("allowed_domains", [])]
        self.limit = int(self.settings.get("max_actions_per_min_per_domain", 6))
        self.artifacts_dir = Path(self.settings.get("artifacts_dir", "artifacts"))
        # trust_env=False: scanning must not inherit proxy/CA placeholders from the caller env.
        self._client = httpx.Client(timeout=15.0, follow_redirects=True, trust_env=False)

    def steps(self, task: dict) -> list[Step]:
        intent = task.get("intent") or {}
        targets = [t for t in (intent.get("targets") or []) if t]
        if not targets:
            text = " ".join(str(task.get(key, "")) for key in ("title", "prompt"))
            text += " " + str(intent.get("prompt", ""))
            targets = extract_urls(text)
        if not targets:
            raise AgentError("policy", "no_target")
        if len(targets) > 10:
            raise AgentError("policy", "too_many_targets")
        return [Step("fetch", url) for url in targets]

    def perform(self, step: Step, ctx: dict) -> str | None:
        if step.kind != "fetch":
            return None
        url = step.target or ""
        host = (urlparse(url).hostname or "").lower()
        if not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed):
            raise AgentError("policy", f"domain_not_allowed:{host}")
        self._rate_gate(host)

        started = time.monotonic()
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            raise AgentError("transient", f"http_error:{type(exc).__name__}") from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 500:
            raise AgentError("transient", f"http_{response.status_code}")

        finding = {
            "url": url,
            "status": response.status_code,
            "elapsed_ms": elapsed_ms,
            "bytes": len(response.content),
            "sha256": hashlib.sha256(response.content).hexdigest(),
            "ok": 200 <= response.status_code < 400,
        }
        return self._write(finding, ctx)

    # ----------------------------------------------------------------- internals
    def _rate_gate(self, host: str) -> None:
        db_path = self.settings.get("db_path")
        if not db_path:
            return
        conn = connect(db_path)
        try:
            if not RateGate(conn, utcnow).allow(host, self.limit):
                raise AgentError("policy", f"rate_limited:{host}")
        finally:
            conn.close()

    def _write(self, finding: dict, ctx: dict) -> str:
        run_dir = self.artifacts_dir / str(ctx.get("run_id", "run"))
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / f"scan-{uuid.uuid4().hex[:8]}.json"
        path.write_text(json.dumps(finding, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)
