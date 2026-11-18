"""Publish text through a signed-in browser session.

Site flows are declarative: `[agents.x-publisher.sites.<name>]` (or the built-in defaults)
list steps such as goto / click / type_text / verify_text / click_optional / wait_optional.
A task picks the site by `intent.site` or by a URL host in the prompt; a site with no spec is
rejected instead of being guessed.
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..browser.driver import BrowserSession
from ..runner.protocol import Agent, AgentError, Step

DEFAULT_SPEC = {
    "compose_url": "https://x.com/compose/post",
    "text_selector": "[data-testid='tweetTextarea_0']",
    "publish_selector": "[data-testid='tweetButtonInline']",
    "confirm_selector": "[data-testid='toast']",
    "max_chars": 280,
}

BUILTIN_SITES = {
    "x": {
        "compose_url": "https://x.com/compose/post",
        "max_chars": 280,
        "ops": [
            {"op": "goto", "target": "compose_url"},
            {"op": "type_text", "selector": "[data-testid='tweetTextarea_0']"},
            {"op": "verify_text", "selector": "[data-testid='tweetTextarea_0']"},
            {"op": "click", "selector": "[data-testid='tweetButtonInline']"},
            {"op": "wait_optional", "selector": "[data-testid='toast']"},
        ],
    },
    "bluesky": {
        "compose_url": "https://bsky.app/",
        "max_chars": 300,
        "ops": [
            {"op": "goto", "target": "compose_url"},
            {"op": "click", "selector": "button[aria-label='Compose new post']"},
            {"op": "type_text", "selector": "div[contenteditable='true']"},
            {"op": "verify_text", "selector": "div[contenteditable='true']"},
            {"op": "click", "selector": "button[aria-label='Publish posts']"},
            {"op": "wait_optional", "selector": "[data-testid='toast']"},
        ],
    },
    "medium": {
        "compose_url": "https://medium.com/new-story",
        "max_chars": 20000,
        "ops": [
            {"op": "goto", "target": "compose_url"},
            {"op": "type_text", "selector": "div[contenteditable='true']"},
            {"op": "verify_text", "selector": "div[contenteditable='true']"},
            {"op": "click", "selector": "button:has-text('Publish')"},
            {"op": "click_optional", "selector": "button:has-text('Publish now')"},
            {"op": "wait_optional", "selector": "[data-testid='toast']"},
        ],
    },
    "linkedin": {
        "compose_url": "https://www.linkedin.com/feed/?shareActive=true",
        "max_chars": 3000,
        "ops": [
            {"op": "goto", "target": "compose_url"},
            {"op": "type_text", "selector": "div[contenteditable='true']"},
            {"op": "verify_text", "selector": "div[contenteditable='true']"},
            {"op": "click", "selector": "button:has-text('Post')"},
            {"op": "wait_optional", "selector": "[role='alert']"},
        ],
    },
}


def normalize(text: str) -> str:
    return " ".join((text or "").split())


def make_session(settings: dict) -> BrowserSession:
    from ..browser.driver import connect

    browser = settings.get("browser", {})
    spec = {**DEFAULT_SPEC, **(settings.get("x_publisher") or {})}
    host = urlparse(spec["compose_url"]).hostname
    page, handle = connect(
        mode=browser.get("mode", "cdp"),
        cdp_endpoint=browser.get("cdp_endpoint", "http://127.0.0.1:9222"),
        profile_dir=browser.get("profile_dir", "profiles/main"),
        preferred_host=host,
    )
    session = BrowserSession(page)
    session.handle = handle  # keep the playwright driver alive
    return session


class XPublisherAgent(Agent):
    name = "x-publisher"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.sites = {**BUILTIN_SITES, **(self.settings.get("sites") or {})}
        self.spec = {**DEFAULT_SPEC, **(self.settings.get("x_publisher") or {})}
        self._session = None
        self._active = self.spec
        self._text = ""

    def steps(self, task: dict) -> list[Step]:
        self._active = self._select_spec(task)
        self._text = (task.get("prompt") or "").strip()
        self._check_domain(self._active["compose_url"])
        if not self._text:
            raise AgentError("policy", "empty_text")
        limit = int(self._active.get("max_chars", 20000))
        if len(self._text) > limit:
            raise AgentError("policy", "text_too_long")
        return [Step(op["op"], self._resolve(op)) for op in self._ops_for(self._active)]

    def perform(self, step: Step, ctx: dict) -> str | None:
        session = self._session_obj()
        text = self._text or (ctx["task"].get("prompt") or "").strip()
        if step.kind == "goto":
            session.goto(step.target or self._active["compose_url"])
            return None
        if step.kind == "click":
            session.click_selector(step.target or "")
            return None
        if step.kind == "click_optional":
            try:
                session.click_selector(step.target or "")
            except Exception:  # noqa: BLE001 - optional confirmation button
                return None
            return None
        if step.kind == "type_text":
            session.type_into(step.target or "", text)
            return None
        if step.kind == "verify_text":
            actual = normalize(session.read_text(step.target or ""))
            if actual != normalize(text):
                raise AgentError("human_required", "content_mismatch")
            return None
        if step.kind == "wait_optional":
            try:
                session.wait_for(step.target or "", 20000)
            except Exception:  # noqa: BLE001 - success does not require the toast
                return None
            return None
        raise AgentError("policy", f"unknown_step:{step.kind}")

    def _ops_for(self, spec: dict) -> list[dict]:
        if spec.get("ops"):
            return spec["ops"]
        ops = [
            {"op": "goto", "target": "compose_url"},
            {"op": "type_text", "selector": spec["text_selector"]},
            {"op": "verify_text", "selector": spec["text_selector"]},
            {"op": "click", "selector": spec["publish_selector"]},
        ]
        if spec.get("confirm_selector"):
            ops.append({"op": "wait_optional", "selector": spec["confirm_selector"]})
        return ops

    def _resolve(self, op: dict) -> str | None:
        if op.get("target") == "compose_url":
            return self._active["compose_url"]
        return op.get("selector") or op.get("target")

    def _select_spec(self, task: dict) -> dict:
        intent = task.get("intent") or {}
        hint = str(intent.get("site") or "").strip().lower()
        if hint:
            if hint in self.sites:
                return {**DEFAULT_SPEC, **self.sites[hint]}
            raise AgentError("policy", f"site_not_configured:{hint}")
        text = " ".join(str(task.get(key, "")) for key in ("title", "prompt"))
        for spec in self.sites.values():
            host = urlparse(spec.get("compose_url", "")).hostname or ""
            if host and host in text:
                return {**DEFAULT_SPEC, **spec}
        return self.spec

    def _check_domain(self, url: str) -> None:
        from .domains import check_domain

        check_domain(url, self.settings)

    def _session_obj(self) -> BrowserSession:
        from ..browser.driver import DriverUnavailable

        if self._session is None:
            try:
                self._session = make_session(self.settings)
            except DriverUnavailable as exc:
                # Missing runtime dependency must not burn retries.
                raise AgentError("policy", str(exc)) from exc
        return self._session
