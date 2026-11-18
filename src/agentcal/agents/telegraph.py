"""Telegraph publishing through its HTTP API (no account, no browser)."""
from __future__ import annotations

import json

import httpx

from ..http import make_client as _http_client
from ..runner.protocol import Agent, AgentError, Step
from .. import secrets

API = "https://api.telegra.ph"


class TelegraphAgent(Agent):
    name = "telegraph"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.token = self.settings.get("telegraph_token") or secrets.get_text("telegraph_token")

    def steps(self, task: dict) -> list[Step]:
        if not self.token:
            raise AgentError("policy", "telegraph_token_missing")
        return [Step("create")]

    def perform(self, step: Step, ctx: dict) -> str | None:
        title = (ctx["task"].get("title") or "AgentCal").strip()[:200]
        text = (ctx["task"].get("prompt") or "").strip()
        if not text:
            raise AgentError("policy", "empty_text")
        content = json.dumps([{"tag": "p", "children": [text]}])
        payload = {
            "access_token": self.token,
            "title": title,
            "author_name": self.settings.get("author_name", "AgentCal"),
            "content": content,
        }
        client = _http_client()
        try:
            response = client.post(f"{API}/createPage", data=payload, timeout=30)
        except httpx.HTTPError as exc:
            raise AgentError("transient", f"telegraph:{type(exc).__name__}") from exc
        if response.status_code >= 500:
            raise AgentError("transient", f"telegraph_http_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise AgentError("transient", "telegraph_bad_json") from exc
        if not data.get("ok"):
            raise AgentError("policy", f"telegraph_error:{data.get('error', 'unknown')}")
        return data["result"]["url"]
