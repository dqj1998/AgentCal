"""VLM grounding: screenshot + instruction -> accessibility target (pixel fallback last)."""
from __future__ import annotations

import base64
import json

import httpx

from .grounding import Target, parse_vlm_target

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "name": {"type": "string"},
        "selector": {"type": "string"},
        "x": {"type": "number"},
        "y": {"type": "number"},
    },
}


class VisionUnavailable(RuntimeError):
    pass


def ground_instruction(
    client: httpx.Client,
    base_url: str,
    model: str,
    instruction: str,
    screenshot: bytes,
    timeout: float = 60.0,
) -> Target:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Locate the UI element for this instruction: "
                    + instruction
                    + ". Reply JSON with role+name, or selector, or x/y pixels. Never guess."
                ),
                "images": [base64.b64encode(screenshot).decode()],
            }
        ],
        "stream": False,
        "format": VISION_SCHEMA,
    }
    try:
        response = client.post(f"{base_url.rstrip('/')}/api/chat", json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise VisionUnavailable(f"{type(exc).__name__}") from exc
    if response.status_code >= 400:
        raise VisionUnavailable(f"http_{response.status_code}")
    content = ((response.json().get("message") or {}).get("content") or "").strip()
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        raise VisionUnavailable("invalid_json") from exc
    target = parse_vlm_target(obj)
    if target is None:
        raise VisionUnavailable("no_target")
    return target
