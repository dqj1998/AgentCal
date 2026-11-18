"""Signed parameter envelope + three-region description merge (design v4.0 §5.2)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime

from .clock import parse_iso

SEP_LOG = "---------- RUN LOG (auto, do not edit) ----------"
SEP_PARAMS = "---------- PARAMS v1 (do not edit) ----------"
MAX_DESCRIPTION = 8192
DEFAULT_BUDGET = 7800

REQUIRED_KEYS = ("v", "tid", "nonce", "exp", "sig")


class PayloadTooLarge(ValueError):
    pass


def canonical_json(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _payload_bytes(envelope: dict) -> bytes:
    return canonical_json({k: v for k, v in envelope.items() if k != "sig"})


def sign(secret: bytes, envelope: dict) -> dict:
    out = dict(envelope)
    out["sig"] = b64e(hmac.new(secret, _payload_bytes(out), hashlib.sha256).digest())
    return out


def verify(secret: bytes, envelope: dict, now: datetime, expected_tid: str | None = None) -> tuple[bool, str]:
    if not isinstance(envelope, dict):
        return False, "not_an_object"
    for key in REQUIRED_KEYS:
        if key not in envelope:
            return False, f"missing_{key}"
    if envelope.get("v") != 1:
        return False, "unsupported_version"
    try:
        expires = parse_iso(str(envelope["exp"]))
    except ValueError:
        return False, "bad_exp"
    if expires <= now:
        return False, "expired"
    if expected_tid and envelope.get("tid") != expected_tid:
        return False, "tid_mismatch"
    expected = b64e(hmac.new(secret, _payload_bytes(envelope), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, str(envelope.get("sig", ""))):
        return False, "bad_signature"
    return True, "ok"


def parse_description(text: str) -> tuple[str, str, str]:
    """Return (prompt, log, params_b64). Missing regions come back as ''."""
    text = text or ""
    params = ""
    if SEP_PARAMS in text:
        head, tail = text.split(SEP_PARAMS, 1)
        params = tail.strip().splitlines()[0].strip() if tail.strip() else ""
    else:
        head = text
    log = ""
    if SEP_LOG in head:
        prompt, log = head.split(SEP_LOG, 1)
    else:
        prompt = head
    return prompt.strip(), log.strip(), params


def render_description(prompt: str, log: str, params: str) -> str:
    return (
        prompt.rstrip()
        + "\n\n"
        + SEP_LOG
        + "\n"
        + log.rstrip()
        + "\n\n"
        + SEP_PARAMS
        + "\n"
        + params
        + "\n"
    )


def merge_description(
    existing: str,
    *,
    prompt: str | None = None,
    log: str | None = None,
    params: str | None = None,
    budget: int = DEFAULT_BUDGET,
) -> str:
    """Replace only the given regions; human prompt survives projector writes."""
    cur_prompt, cur_log, cur_params = parse_description(existing)
    merged = render_description(
        cur_prompt if prompt is None else prompt,
        cur_log if log is None else log,
        cur_params if params is None else params,
    )
    if len(merged) <= min(budget, MAX_DESCRIPTION):
        return merged

    lines = (cur_log if log is None else log).splitlines()
    while lines and len(merged) > budget:
        lines = lines[1:]
        merged = render_description(
            cur_prompt if prompt is None else prompt,
            "\n".join(lines),
            cur_params if params is None else params,
        )
    if len(merged) > min(budget, MAX_DESCRIPTION):
        raise PayloadTooLarge(f"description exceeds {min(budget, MAX_DESCRIPTION)} chars after truncation")
    return merged
