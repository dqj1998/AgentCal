# src/agentcal/browser

## OVERVIEW
Browser execution helpers (score 10, distinct domain): CDP attach to the user's Chrome, one-profile-one-lock, humanized input, and a11y-first vision grounding. Pages sit behind a `Page` Protocol so tests run on a fake.

## WHERE TO LOOK
| Concern | File |
|---|---|
| Session API, CDP/persistent connect, tab choice | driver.py (`BrowserSession`, `PlaywrightPage`, `connect`, `pick_page`) |
| Profile lock and queueing | profile.py (`ProfileLease`, `ProfileBusy`) |
| Typing plan, delays, Bezier paths | humanizer.py (`typing_plan`, `key_delay`, `bezier_points`) |
| VLM target parsing and pixel conversion | grounding.py (`Target`, `parse_vlm_target`, `to_css`) |
| VLM call | vision.py (`ground_instruction`) |

## CONVENTIONS
- Default mode is CDP attach to `127.0.0.1:9222` (ADR 0003); `persistent` is a fallback only.
- `connect()` returns `(PlaywrightPage, handle)`; the caller must keep `handle` alive or the session dies.
- `pick_page` reuses a tab whose host matches (`owned=False`); borrowed tabs must never be closed.
- Clicking always hovers first. Text longer than 120 chars is pasted (`insert_text`), not typed.
- Humanizer functions are pure and take a seeded `random.Random`; tests pass `sleep=lambda s: None` to avoid real delays.
- Grounding order: a11y `(role, name)` -> selector -> pixels; `to_css` divides by DPR and subtracts `scrollY`.
- Tabs opened by a run persist in the shared Chrome window; close them when a batch finishes.

## ANTI-PATTERNS
- NEVER reuse `context.pages[0]`; on a shared CDP instance that tab may belong to another tool.
- NEVER launch a second Chrome while in CDP mode: one user-data-dir allows one instance.
- NEVER import playwright at module scope; raise `DriverUnavailable("playwright_missing")` and let agents map it to `AgentError("policy", ...)` so retries are not burned.
- NEVER add real sleeps, wall-clock reads, or unseeded randomness to humanizer/grounding math.
- NEVER click raw pixel coordinates without DPR and scroll conversion.
