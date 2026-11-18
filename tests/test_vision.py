import json
import unittest

import httpx

from agentcal.browser.vision import VisionUnavailable, ground_instruction


def client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class VisionTest(unittest.TestCase):
    def test_prefers_accessibility_target(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            content = json.dumps({"role": "button", "name": "稍后提醒", "x": 400, "y": 600})
            return httpx.Response(200, json={"message": {"content": content}})

        target = ground_instruction(client_with(handler), "http://127.0.0.1:11434", "vlm", "点击稍后提醒", b"PNG")
        self.assertEqual(target.kind, "a11y")
        self.assertEqual(target.name, "稍后提醒")
        self.assertEqual(len(captured["messages"][0]["images"]), 1)
        self.assertEqual(captured["format"]["type"], "object")

    def test_pixel_fallback_is_returned_as_is(self):
        def handler(request):
            return httpx.Response(200, json={"message": {"content": json.dumps({"x": 400, "y": 600})}})

        target = ground_instruction(client_with(handler), "http://x", "vlm", "click", b"PNG")
        self.assertEqual((target.kind, target.px, target.py), ("point", 400.0, 600.0))

    def test_invalid_json_raises(self):
        def handler(request):
            return httpx.Response(200, json={"message": {"content": "no idea"}})

        with self.assertRaises(VisionUnavailable):
            ground_instruction(client_with(handler), "http://x", "vlm", "click", b"PNG")

    def test_http_error_raises(self):
        def handler(request):
            return httpx.Response(500, json={})

        with self.assertRaises(VisionUnavailable):
            ground_instruction(client_with(handler), "http://x", "vlm", "click", b"PNG")
