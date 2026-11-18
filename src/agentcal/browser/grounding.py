"""Vision fallback: prefer accessibility targets, convert pixels to CSS coordinates."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    kind: str  # a11y | selector | point
    role: str | None = None
    name: str | None = None
    selector: str | None = None
    px: float | None = None
    py: float | None = None


def parse_vlm_target(obj: dict) -> Target | None:
    if obj.get("role") and obj.get("name"):
        return Target("a11y", role=str(obj["role"]), name=str(obj["name"]))
    if obj.get("selector"):
        return Target("selector", selector=str(obj["selector"]))
    if obj.get("x") is not None and obj.get("y") is not None:
        return Target("point", px=float(obj["x"]), py=float(obj["y"]))
    return None


def to_css(px: float, py: float, device_pixel_ratio: float, scroll_y: float = 0.0) -> tuple[float, float]:
    if device_pixel_ratio <= 0:
        raise ValueError("device_pixel_ratio must be positive")
    return px / device_pixel_ratio, py / device_pixel_ratio - scroll_y


def within(box: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    left, top, width, height = box
    x, y = point
    return left <= x <= left + width and top <= y <= top + height
