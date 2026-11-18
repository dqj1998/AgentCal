"""Blogger publishing: iframe body via execCommand, pointer-event publish, text confirm."""
from __future__ import annotations

import html
import json
from urllib.parse import urlparse

from ..browser.driver import BrowserSession
from ..runner.protocol import Agent, AgentError, Step

DEFAULT_SPEC = {"blog_id": "1526695234663545459"}

NEW_POST = "div:has-text('\u65b0\u3057\u3044\u6295\u7a3f'), div:has-text('New post')"
TITLE = "input[aria-label='\u30bf\u30a4\u30c8\u30eb'], input[aria-label='Title']"

FIRE_PUBLISH = """(() => {
  const el = [...document.querySelectorAll('[aria-label]')].find(e => {
    const l = e.getAttribute('aria-label') || '';
    return l.includes('\u516c\u958b') || l === 'Publish' || l === 'Publish post';
  });
  if (!el) return 'notfound';
  const r = el.getBoundingClientRect();
  ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t =>
    el.dispatchEvent(new MouseEvent(t, {bubbles:true, cancelable:true, clientX:r.x+r.width/2, clientY:r.y+r.height/2, view:window})));
  return 'fired';
})()"""

FIRE_CONFIRM = """(() => {
  const btn = [...document.querySelectorAll('button, div[role=button]')].find(e => {
    const t = (e.innerText || '').trim();
    return t === '\u78ba\u5b9a' || t === 'Confirm' || t === 'OK';
  });
  if (!btn) return 'notfound';
  btn.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
  return 'fired';
})()"""


def make_session(settings: dict) -> BrowserSession:
    from ..browser.driver import connect

    browser = settings.get("browser", {})
    page, handle = connect(
        mode=browser.get("mode", "cdp"),
        cdp_endpoint=browser.get("cdp_endpoint", "http://127.0.0.1:9222"),
        profile_dir=browser.get("profile_dir", "profiles/main"),
        preferred_host="blogger.com",
    )
    session = BrowserSession(page)
    session.handle = handle
    return session


def as_html(text: str) -> str:
    body = "".join("<p>" + html.escape(line) + "</p>" for line in text.splitlines() if line.strip())
    return body or "<p></p>"


class BloggerAgent(Agent):
    name = "blogger"

    def __init__(self, settings: dict | None = None):
        self.settings = settings or {}
        self.spec = {**DEFAULT_SPEC, **(self.settings.get("blogger") or {})}
        self._session = None
        self._title = ""
        self._text = ""

    def steps(self, task: dict) -> list[Step]:
        self._check_domain()
        self._title = (task.get("title") or "AgentCal").strip()[:200]
        self._text = (task.get("prompt") or "").strip()
        if not self._text:
            raise AgentError("policy", "empty_text")
        return [
            Step("goto", "https://www.blogger.com/blog/posts/" + str(self.spec["blog_id"])),
            Step("click", NEW_POST),
            Step("wait_optional", TITLE),
            Step("title", TITLE),
            Step("body", "iframe"),
            Step("publish", "1"),
            Step("publish", "2"),
            Step("confirm", "confirm"),
            Step("wait_optional", "a[href*='/post/edit/']"),
        ]

    def perform(self, step: Step, ctx: dict) -> str | None:
        session = self._session_obj()
        if step.kind == "goto":
            session.goto(step.target or "")
            return None
        if step.kind == "click":
            session.click_selector(step.target or "")
            return None
        if step.kind == "wait_optional":
            try:
                session.wait_for(step.target or "", 20000)
            except Exception:  # noqa: BLE001 - editor/list may already be visible
                return None
            return None
        if step.kind == "title":
            session.type_into(step.target or TITLE, self._title)
            return None
        if step.kind == "body":
            result = session.evaluate(
                """(() => {
  const f = [...document.querySelectorAll('iframe')].find(x => {
    try { return x.contentDocument.body.getAttribute('g_editable') === 'true'; } catch (e) { return false; }
  });
  if (!f) return 'no-frame';
  const d = f.contentDocument, b = d.body;
  b.focus();
  const r = d.createRange(); r.selectNodeContents(b);
  const s = d.getSelection(); s.removeAllRanges(); s.addRange(r);
  d.execCommand('insertHTML', false, %s);
  return 'ok';
})()"""
                % json.dumps(as_html(self._text))
            )
            if result != "ok":
                raise AgentError("human_required", "blogger_body_frame_missing")
            return None
        if step.kind == "publish":
            if session.evaluate(FIRE_PUBLISH) == "notfound":
                raise AgentError("human_required", "blogger_publish_button_missing")
            return None
        if step.kind == "confirm":
            session.evaluate(FIRE_CONFIRM)
            return None
        raise AgentError("policy", f"unknown_step:{step.kind}")

    def _check_domain(self) -> None:
        from .domains import check_domain

        check_domain("https://www.blogger.com/", self.settings)

    def _session_obj(self) -> BrowserSession:
        from ..browser.driver import DriverUnavailable

        if self._session is None:
            try:
                self._session = make_session(self.settings)
            except DriverUnavailable as exc:
                raise AgentError("policy", str(exc)) from exc
        return self._session
