"""Humanizer math: delays, typos, Bezier paths. Pure functions, seeded RNG."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

CONFUSABLE = {"a": "s", "e": "r", "t": "y", "o": "p", "i": "u", "n": "m", "l": "k", "s": "a"}
PASTE_THRESHOLD_CHARS = 120


@dataclass(frozen=True)
class TypingProfile:
    mean_ms: float = 120.0
    sigma_ms: float = 35.0
    min_ms: float = 55.0
    max_ms: float = 320.0
    punct_pause_ms: tuple[int, int] = (250, 600)
    typo_rate: float = 0.0


@dataclass(frozen=True)
class Action:
    kind: str  # press | backspace | sleep_ms | paste
    value: str | int | None = None


def key_delay(rng: random.Random, profile: TypingProfile) -> int:
    value = rng.gauss(profile.mean_ms, profile.sigma_ms)
    return int(min(max(value, profile.min_ms), profile.max_ms))


def typing_plan(text: str, rng: random.Random, profile: TypingProfile = TypingProfile()) -> list[Action]:
    if len(text) > PASTE_THRESHOLD_CHARS:
        return [Action("paste", text)]
    plan: list[Action] = []
    for char in text:
        if profile.typo_rate > 0 and char.lower() in CONFUSABLE and rng.random() < profile.typo_rate:
            plan.append(Action("press", CONFUSABLE[char.lower()]))
            plan.append(Action("sleep_ms", key_delay(rng, profile)))
            plan.append(Action("backspace"))
        plan.append(Action("press", char))
        delay = key_delay(rng, profile)
        if char in ".,;:!?":
            delay += rng.randint(*profile.punct_pause_ms)
        plan.append(Action("sleep_ms", delay))
    return plan


def bezier_points(
    start: tuple[float, float],
    end: tuple[float, float],
    rng: random.Random,
    n: int = 24,
    jitter_px: float = 1.5,
    overshoot_px: float = 12.0,
) -> list[tuple[float, float]]:
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length, dx / length
    bend = rng.uniform(-0.25, 0.25) * length
    c1 = (x0 + dx * 0.33 + nx * bend, y0 + dy * 0.33 + ny * bend)
    c2 = (x0 + dx * 0.66 - nx * bend * 0.5, y0 + dy * 0.66 - ny * bend * 0.5)
    over = (x1 + dx / length * overshoot_px, y1 + dy / length * overshoot_px)
    points: list[tuple[float, float]] = []
    for step in range(n + 1):
        t = step / n
        mt = 1 - t
        px = mt**3 * x0 + 3 * mt**2 * t * c1[0] + 3 * mt * t**2 * c2[0] + t**3 * over[0]
        py = mt**3 * y0 + 3 * mt**2 * t * c1[1] + 3 * mt * t**2 * c2[1] + t**3 * over[1]
        if step not in (0, n):
            px += rng.uniform(-jitter_px, jitter_px)
            py += rng.uniform(-jitter_px, jitter_px)
        points.append((px, py))
    points.append((x1, y1))
    return points
