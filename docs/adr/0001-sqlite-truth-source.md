# ADR 0001: SQLite is the only source of truth

Status: accepted (2026-11-17)

## Context

v3.0 treated Google Calendar as the task state store: poll the event, execute, write colorId/description back.
Calendar is slow, eventually consistent and freely editable by humans, so it cannot serve as a transaction log.

## Decision

- `tasks/runs/steps/commands/approvals` live in SQLite (WAL). Every state transition is a local transaction.
- Calendar and Telegram are projections plus input channels.
- A projection failure never blocks or rolls back task progress; it lands in `projection_outbox`.
- The FS must never read `colorId` to decide a state.

## Consequences

- Reconcile on startup is mandatory: expired leases become `orphaned`, unknown side effects become `blocked_human`.
- Projections need self-write filtering (`projected_events`), otherwise the poller feeds on its own writes.
