import unittest

from agentcal.agents.blogger import TITLE, BloggerAgent, as_html
from agentcal.browser.driver import BrowserSession
from agentcal.runner.protocol import AgentError


class FakePage:
    device_pixel_ratio = 2.0
    scroll_y = 0.0

    def __init__(self, evaluate_map=None):
        self.calls = []
        self.text = ""
        self.evaluate_map = evaluate_map or {}

    def goto(self, url):
        self.calls.append(("goto", url))

    def focus(self, selector):
        self.calls.append(("focus", selector))

    def type_char(self, char):
        self.text += char

    def press_backspace(self):
        self.text = self.text[:-1]

    def insert_text(self, text):
        self.text += text

    def click(self, selector):
        self.calls.append(("click", selector))

    def hover(self, selector):
        pass

    def inner_text(self, selector):
        return self.text

    def wait_for(self, selector, timeout_ms):
        self.calls.append(("wait", selector))

    def screenshot(self):
        return b""

    def click_by_role(self, role, name):
        pass

    def mouse_click(self, x, y):
        pass

    def evaluate(self, script):
        if "g_editable" in script:
            return self.evaluate_map.get("body", "ok")
        if "pointerdown" in script:
            return self.evaluate_map.get("publish", "fired")
        return self.evaluate_map.get("confirm", "fired")


class HtmlTest(unittest.TestCase):
    def test_escapes_markup(self):
        out = as_html("a < b & c")
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)
        self.assertNotIn("< b", out)

    def test_blank_lines_are_dropped(self):
        self.assertEqual(as_html("\n\n"), "<p></p>")


class BloggerTest(unittest.TestCase):
    def _agent(self, **settings):
        agent = BloggerAgent(settings)
        agent._session = BrowserSession(FakePage(settings.pop("evaluate_map", None)))
        return agent

    def test_steps_cover_editor_and_publish(self):
        agent = self._agent()
        kinds = [s.kind for s in agent.steps({"title": "T", "prompt": "body"})]
        self.assertEqual(kinds[0], "goto")
        self.assertIn("body", kinds)
        self.assertEqual(kinds.count("publish"), 2)
        self.assertIn("confirm", kinds)

    def test_foreign_domain_asks_for_approval(self):
        agent = BloggerAgent({"allowed_domains": ["example.com"]})
        with self.assertRaises(AgentError) as ctx:
            agent.steps({"title": "T", "prompt": "body"})
        self.assertEqual(ctx.exception.error_class, "human_required")
        self.assertIn("new_domain:www.blogger.com", str(ctx.exception))

    def test_missing_editor_frame_blocks_for_human(self):
        agent = self._agent()
        agent._session.page.evaluate_map = {"body": "no-frame"}
        with self.assertRaises(AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "body", "target": "iframe"})(), {"task": {"prompt": "x"}})
        self.assertEqual(ctx.exception.error_class, "human_required")

    def test_missing_publish_button_blocks_for_human(self):
        agent = self._agent()
        agent._session.page.evaluate_map = {"publish": "notfound"}
        with self.assertRaises(AgentError) as ctx:
            agent.perform(type("S", (), {"kind": "publish", "target": "1"})(), {"task": {"prompt": "x"}})
        self.assertEqual(ctx.exception.error_class, "human_required")

    def test_happy_path_types_title_and_body(self):
        agent = self._agent()
        task = {"title": "Hello", "prompt": "line one\nline two"}
        for step in agent.steps(task):
            agent.perform(step, {"task": task})
        page = agent._session.page
        self.assertEqual(page.text, "Hello")
        self.assertTrue(any(call[0] == "goto" and "1526695234663545459" in call[1] for call in page.calls))
