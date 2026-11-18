import random
import unittest

from agentcal.browser.grounding import Target, parse_vlm_target, to_css, within
from agentcal.browser.humanizer import (
    PASTE_THRESHOLD_CHARS,
    TypingProfile,
    bezier_points,
    key_delay,
    typing_plan,
)


class HumanizerTest(unittest.TestCase):
    def test_key_delay_within_bounds(self):
        rng = random.Random(1)
        profile = TypingProfile()
        for _ in range(200):
            delay = key_delay(rng, profile)
            self.assertGreaterEqual(delay, profile.min_ms)
            self.assertLessEqual(delay, profile.max_ms)

    def test_long_text_uses_paste(self):
        plan = typing_plan("x" * (PASTE_THRESHOLD_CHARS + 1), random.Random(2))
        self.assertEqual([a.kind for a in plan], ["paste"])

    def test_punctuation_adds_pause(self):
        plan = typing_plan("a.", random.Random(3), TypingProfile(typo_rate=0.0))
        sleeps = [a.value for a in plan if a.kind == "sleep_ms"]
        self.assertEqual(len(sleeps), 2)
        self.assertGreaterEqual(sleeps[1], 250)

    def test_typo_then_backspace(self):
        plan = typing_plan("a", random.Random(4), TypingProfile(typo_rate=1.0))
        kinds = [a.kind for a in plan]
        self.assertIn("backspace", kinds)
        self.assertEqual(kinds[-1], "sleep_ms")

    def test_bezier_starts_at_start_and_ends_at_target(self):
        points = bezier_points((0.0, 0.0), (100.0, 50.0), random.Random(5), n=20)
        self.assertEqual(points[0], (0.0, 0.0))
        self.assertEqual(points[-1], (100.0, 50.0))
        self.assertGreater(len(points), 20)
        for x, y in points[1:-1]:
            self.assertGreater(x, -20)
            self.assertLess(x, 140)


class GroundingTest(unittest.TestCase):
    def test_a11y_beats_selector_and_point(self):
        target = parse_vlm_target({"role": "button", "name": "稍后提醒", "selector": "#x", "x": 1, "y": 2})
        self.assertEqual(target.kind, "a11y")
        self.assertEqual(target.name, "稍后提醒")

    def test_point_conversion_uses_dpr_and_scroll(self):
        x, y = to_css(400.0, 600.0, device_pixel_ratio=2.0, scroll_y=100.0)
        self.assertEqual((x, y), (200.0, 200.0))

    def test_invalid_dpr_rejected(self):
        with self.assertRaises(ValueError):
            to_css(1, 1, 0)

    def test_within_bounds(self):
        self.assertTrue(within((10, 10, 100, 40), (50, 30)))
        self.assertFalse(within((10, 10, 100, 40), (200, 30)))
