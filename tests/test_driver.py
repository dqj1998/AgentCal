import unittest

from agentcal.browser.driver import BrowserSession, pick_page
from agentcal.browser.grounding import Target


class FakePage:
    device_pixel_ratio = 2.0
    scroll_y = 50.0

    def __init__(self, *, click_fails=False, text=""):
        self.calls = []
        self.text = text
        self.click_fails = click_fails

    def goto(self, url):
        self.calls.append(("goto", url))

    def focus(self, selector):
        self.calls.append(("focus", selector))

    def type_char(self, char):
        self.text += char

    def press_backspace(self):
        self.text = self.text[:-1]
        self.calls.append(("backspace",))

    def insert_text(self, text):
        self.text += text
        self.calls.append(("insert", len(text)))

    def click(self, selector):
        if self.click_fails:
            raise TimeoutError("selector not clickable")
        self.calls.append(("click", selector))

    def click_by_role(self, role, name):
        self.calls.append(("role", role, name))

    def mouse_click(self, x, y):
        self.calls.append(("mouse", x, y))

    def hover(self, selector):
        self.calls.append(("hover", selector))

    def inner_text(self, selector):
        return self.text

    def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait", selector))

    def screenshot(self):
        return b"PNG"


class _FakeTab:
    def __init__(self, url):
        self.url = url


class _FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.created = []

    def new_page(self):
        tab = _FakeTab("about:blank")
        self.created.append(tab)
        self.pages.append(tab)
        return tab


class PickPageTest(unittest.TestCase):
    def test_reuses_tab_on_same_host(self):
        mine = _FakeTab("https://x.com/compose/post")
        context = _FakeContext([_FakeTab("https://news.ycombinator.com/"), mine])
        page, owned = pick_page(context, "x.com")
        self.assertIs(page, mine)
        self.assertFalse(owned)

    def test_opens_new_tab_when_no_match(self):
        context = _FakeContext([_FakeTab("https://other.example/")])
        page, owned = pick_page(context, "x.com")
        self.assertTrue(owned)
        self.assertIn(page, context.created)

    def test_host_suffix_is_not_a_false_positive(self):
        context = _FakeContext([_FakeTab("https://notx.com/")])
        _page, owned = pick_page(context, "x.com")
        self.assertTrue(owned)

    def test_without_host_always_opens_new(self):
        context = _FakeContext([_FakeTab("https://x.com/")])
        _page, owned = pick_page(context, None)
        self.assertTrue(owned)


class DriverTest(unittest.TestCase):
    def _session(self, page, **kwargs):
        return BrowserSession(page, sleep=lambda seconds: None, **kwargs)

    def test_typing_plan_reproduces_text(self):
        page = FakePage()
        self._session(page).type_into("#box", "hello, world!")
        self.assertEqual(page.text, "hello, world!")

    def test_long_text_uses_insert_text(self):
        page = FakePage()
        self._session(page).type_into("#box", "x" * 300)
        self.assertEqual(page.text, "x" * 300)
        self.assertTrue(any(call[0] == "insert" for call in page.calls))

    def test_click_selector_hovers_first(self):
        page = FakePage()
        self._session(page).click_selector("#go")
        self.assertEqual(page.calls[-2:], [("hover", "#go"), ("click", "#go")])

    def test_point_target_uses_dpr_and_scroll(self):
        page = FakePage()
        self._session(page).click_target(Target("point", px=400.0, py=600.0))
        self.assertEqual(page.calls[-1], ("mouse", 200.0, 250.0))

    def test_vision_fallback_prefers_a11y(self):
        page = FakePage(click_fails=True)
        session = self._session(page, vision=lambda shot, instruction: Target("a11y", role="button", name="稍后提醒"))
        session.click_or_ground("#missing", "点击稍后提醒")
        self.assertEqual(page.calls[-1], ("role", "button", "稍后提醒"))

    def test_without_vision_the_original_error_surfaces(self):
        page = FakePage(click_fails=True)
        with self.assertRaises(TimeoutError):
            self._session(page).click_or_ground("#missing", "click")
