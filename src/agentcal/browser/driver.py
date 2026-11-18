"""Playwright adapter and humanized session. Page protocol is fake-able for tests."""
from __future__ import annotations

import time
from typing import Callable, Protocol
from urllib.parse import urlparse

from .grounding import Target, to_css
from .humanizer import TypingProfile, typing_plan


class DriverUnavailable(RuntimeError):
    pass


class Page(Protocol):
    device_pixel_ratio: float
    scroll_y: float

    def goto(self, url: str) -> None: ...
    def focus(self, selector: str) -> None: ...
    def type_char(self, char: str) -> None: ...
    def press_backspace(self) -> None: ...
    def insert_text(self, text: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def click_by_role(self, role: str, name: str) -> None: ...
    def mouse_click(self, x: float, y: float) -> None: ...
    def hover(self, selector: str) -> None: ...
    def inner_text(self, selector: str) -> str: ...
    def wait_for(self, selector: str, timeout_ms: int) -> None: ...
    def screenshot(self) -> bytes: ...
    def evaluate(self, script: str): ...


class BrowserSession:
    def __init__(
        self,
        page: Page,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng=None,
        profile: TypingProfile | None = None,
        vision: Callable[[bytes, str], Target] | None = None,
    ):
        import random

        self.page = page
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.profile = profile or TypingProfile()
        self.vision = vision

    def goto(self, url: str) -> None:
        self.page.goto(url)

    def type_into(self, selector: str, text: str) -> None:
        self.page.focus(selector)
        for action in typing_plan(text, self.rng, self.profile):
            if action.kind == "press":
                self.page.type_char(str(action.value))
            elif action.kind == "backspace":
                self.page.press_backspace()
            elif action.kind == "sleep_ms":
                self.sleep(float(action.value) / 1000.0)
            elif action.kind == "paste":
                self.page.insert_text(str(action.value))

    def evaluate(self, script: str):
        return self.page.evaluate(script)

    def click_selector(self, selector: str) -> None:
        self.page.hover(selector)
        self.page.click(selector)

    def click_target(self, target: Target) -> None:
        if target.kind == "a11y" and target.role and target.name:
            self.page.click_by_role(target.role, target.name)
        elif target.kind == "selector" and target.selector:
            self.click_selector(target.selector)
        elif target.kind == "point" and target.px is not None and target.py is not None:
            x, y = to_css(
                target.px,
                target.py,
                self.page.device_pixel_ratio,
                self.page.scroll_y,
            )
            self.page.mouse_click(x, y)
        else:
            raise ValueError(f"unusable target: {target}")

    def click_or_ground(self, selector: str, instruction: str) -> None:
        """DOM first; on failure ask the VLM, which prefers a11y over pixel coordinates."""
        try:
            self.click_selector(selector)
            return
        except Exception as exc:  # noqa: BLE001 - any page failure falls back
            if self.vision is None:
                raise
            target = self.vision(self.page.screenshot(), instruction)
            self.click_target(target)

    def read_text(self, selector: str) -> str:
        return self.page.inner_text(selector)

    def wait_for(self, selector: str, timeout_ms: int = 20000) -> None:
        self.page.wait_for(selector, timeout_ms)

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    def close(self) -> None:
        """Close only tabs this session opened; never touch tabs it borrowed."""
        close = getattr(self.page, "close", None)
        if callable(close) and getattr(self.page, "owned", True):
            close()


class PlaywrightPage:
    """Thin adapter over playwright.sync_api.Page."""

    def __init__(self, page, owned: bool = True):
        self._page = page
        self.owned = owned

    @property
    def device_pixel_ratio(self) -> float:
        return float(self._page.evaluate("window.devicePixelRatio") or 1.0)

    @property
    def scroll_y(self) -> float:
        return float(self._page.evaluate("window.scrollY") or 0.0)

    def goto(self, url: str) -> None:
        self._page.goto(url, wait_until="domcontentloaded")

    def focus(self, selector: str) -> None:
        self._page.locator(selector).first.focus()

    def type_char(self, char: str) -> None:
        self._page.keyboard.type(char)

    def press_backspace(self) -> None:
        self._page.keyboard.press("Backspace")

    def insert_text(self, text: str) -> None:
        self._page.keyboard.insert_text(text)

    def click(self, selector: str) -> None:
        self._page.locator(selector).first.click()

    def click_by_role(self, role: str, name: str) -> None:
        self._page.get_by_role(role, name=name).first.click()

    def mouse_click(self, x: float, y: float) -> None:
        self._page.mouse.click(x, y)

    def hover(self, selector: str) -> None:
        self._page.locator(selector).first.hover()

    def inner_text(self, selector: str) -> str:
        return self._page.locator(selector).first.inner_text()

    def wait_for(self, selector: str, timeout_ms: int) -> None:
        self._page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)

    def screenshot(self) -> bytes:
        return self._page.screenshot()

    def evaluate(self, script: str):
        return self._page.evaluate(script)

    def close(self) -> None:
        self._page.close()


def pick_page(context, preferred_host: str | None = None):
    """Reuse a tab already on the target host; otherwise open one.

    Never reuse ``pages[0]``: on a shared CDP instance that tab may belong to
    another tool (this exact mistake happens when 9222 is shared).
    """
    if preferred_host:
        host = preferred_host.lower()
        for page in getattr(context, "pages", []):
            url = getattr(page, "url", "") or ""
            page_host = (urlparse(url).hostname or "").lower()
            if page_host == host or page_host.endswith("." + host):
                return page, False
    return context.new_page(), True


def connect(
    mode: str = "cdp",
    cdp_endpoint: str = "http://127.0.0.1:9222",
    profile_dir: str = "profiles/main",
    preferred_host: str | None = None,
):
    """Returns (PlaywrightPage, playwright handle). Handle must be kept alive."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DriverUnavailable("playwright_missing") from exc
    handle = sync_playwright().start()
    if mode == "cdp":
        browser = handle.chromium.connect_over_cdp(cdp_endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
    else:
        context = handle.chromium.launch_persistent_context(profile_dir, headless=False)
    page, owned = pick_page(context, preferred_host)
    return PlaywrightPage(page, owned=owned), handle
