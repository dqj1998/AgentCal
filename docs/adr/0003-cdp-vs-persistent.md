# ADR 0003: Browser mode A (CDP attach) as the default

Status: accepted (2026-11-17), decision D2; amended same day: share the existing CDP instance

## Context

Chromium's `SingletonLock` allows one live instance per user-data-dir, so a profile is a lock, not a pool.
Persistent Playwright contexts diverge from a real user's fingerprint; stealth patches age quickly against
Playwright releases.

## Decision

- Default: attach to the user's **existing** Chrome debug instance on `127.0.0.1:9222` (shared with other
  local tools that are not running concurrently). No second Chrome is launched.
- Because the instance is shared, AgentCal **never** reuses `context.pages[0]`; it picks a tab whose host
  matches the target site (or opens a new one). Picking page 0 hijacked another tool's tab in practice.
- The local `flock` (`browser/profile.py`) only serialises AgentCal's own runs; it cannot constrain other
  CDP clients. Concurrency with other tools is a usage rule, not an enforced guarantee.
- `mode = "persistent"` exists only as a fallback and must pass a fingerprint self-check before being trusted.
- One executor worker per profile; a second run queues instead of racing for the lock.
- Session expiry is `blocked_human(session_expired)`; the human logs in once, the task resumes from its journal.

## Consequences

- Chrome must stay running (`caffeinate` on laptops); sleep/wake is handled by tick catch-up, not by Chrome.
- Tabs opened by a run persist in the shared window; close them if they accumulate.
- The stealth-patch evaluation from the design doc (M3) is only needed if this ADR is ever revisited.
