# src/agentcal

## OVERVIEW
Runtime package (score 15): intake -> commands -> scheduler -> FSM -> executor -> agents. Read the root file first; this file holds the package-wide state, error, and policy contracts.

## WHERE TO LOOK
| Concern | Modules |
|---|---|
| Foundation (config, DB, time, crypto) | config.py, db.py, clock.py, vault.py, secrets.py |
| Signed intake | envelope.py (HMAC, 3-region description), calendar_client.py, poller.py |
| Authorization | policy.py (owner/signature, per-agent domains, RateGate), auth.py (OAuth PKCE + Keychain) |
| State and scheduling | fsm.py, scheduler.py, supervisor.py |
| Execution | executor.py (journal, steps, artifacts), runner/ |
| Projection out | projector.py (colorId + run log), notifier.py, telegram.py |
| Agents | agents/__init__.py REGISTRY, NEEDS_PROFILE |
| Intent routing and voice | llm/router.py, llm/schemas.py, voice.py |
| Commands/entry | cli.py, __main__.py |

## STATE CONTRACT
Task states: `pending | claimed | running | blocked_human | success | failed | canceled`.
Run states: `leased | running | blocked | succeeded | failed | orphaned`.
- At most one active run per task: partial unique index `ux_tasks_active_run`.
- `claim` is a CAS on `state='pending' AND not_before<=now`; attempt increments at claim time.
- `transient` failure retries with backoff 30/120/480 s while `attempt < max_attempts`; otherwise the task fails.
- `human_required` / `unknown_side_effect` -> `blocked_human` + approval (TTL 86400 s).
- Approve resumes the blocked run when it still exists, else requeues; deny -> `canceled`; `retry` resets attempt to 0 for failed/canceled/blocked.
- Reconcile on every tick: expire approvals, turn expired leases into `orphaned`, and block as `unknown_side_effect` only when a `publish` step already started/finished; otherwise retry or fail.
- Missed tasks (`pending`, `attempt=0`, past `due_at + grace_s`) follow `missed_policy`: `run_late` requeues, `ask` blocks.

## CONVENTIONS
- Error classes are a closed vocabulary: `transient | policy | human_required | unknown_side_effect`. Agents raise `AgentError(error_class, message)`; the worker converts anything else to `transient`.
- All task writes go through `Fsm` inside `db.tx`; enqueue projection and notification rows in the same transaction.
- Calendar input is untrusted: verify HMAC when PARAMS exist, require the owner/calendar organizer, and reject cancelled events before creating a task.
- Per-agent domains win over `[policy].allowed_domains`; DB `trusted_domains` are permanent approvals written on `approve` for `new_domain:<host>`.
- The LLM router is optional: the calendar path falls back to `core.default_agent` when the model is unreachable; a Telegram classification error drops only that update.
- Notifications and projections are best-effort outboxes with exponential backoff; they never propagate failure into the FSM.

## ANTI-PATTERNS
- NEVER update `tasks`/`runs` directly from a channel, projector, notifier, or agent.
- NEVER decide state from Telegram replies or any external echo; use local rows only.
- NEVER bypass the policy gate for Telegram commands, voice input, or calendar events.
- NEVER seal plaintext payloads into `intent_enc`; use `Vault.seal`.
- NEVER let an optional integration (LLM, Telegram, calendar, whisper) make the supervisor tick fatal.
