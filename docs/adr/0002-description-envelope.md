# ADR 0002: Signed envelope in the calendar description

Status: accepted (2026-11-17)

## Context

v3.0 called base64 encoding "tamper proof". It is not: base64 is readable and editable by anyone with
write access to the event, and the description field is replaced wholesale on every PATCH.

## Decision

Three regions separated by fixed markers:

1. human prompt (editable, never touched by the projector),
2. run log (projector-owned, truncated from the top when over budget),
3. `PARAMS v1` line: base64url(JSON) with `{v, tid, nonce, exp, agent, payload_ref, sig}`,
   `sig = HMAC-SHA256(canonical_json)`.

Verification failures yield `reject:<reason>` and no task. Writes use `If-Match`; HTTP 412 triggers
a re-read and merge, at most three times.

## Consequences

- Secrets stay local: `payload_ref` points into the encrypted vault, never inline parameters.
- Budget guard: total description target <= 7800 chars, hard cap 8192; log is truncated first.
