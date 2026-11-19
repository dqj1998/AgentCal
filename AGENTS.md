# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-19
**Commit:** 52df185
**Branch:** main

## OVERVIEW
AgentCal is a local-first, single-user, calendar-driven automation platform: Google Calendar events are untrusted task input, SQLite is the only truth source, Telegram is the intervention console, and agents run in a subprocess. Python 3.12 stdlib + httpx + cryptography; browser work attaches to the user's existing Chrome over CDP.

## STRUCTURE
```
AgentCal/
├── src/agentcal/       # runtime package; entry: python -m agentcal
│   ├── cli.py          # argparse commands + serve loop
│   ├── fsm.py          # only writer of task/run state
│   ├── poller.py       # calendar sync -> commands
│   ├── projector.py    # state -> calendar colorId/description
│   ├── runner/         # subprocess JSON-lines runner + agent Protocol
│   ├── agents/         # code registry + declarative site flows
│   ├── browser/        # CDP attach, profile lock, humanizer, grounding
│   └── migrations/     # numbered append-only SQL
├── tests/              # stdlib unittest, fully offline
├── config/             # example TOML; config/agentcal.toml is gitignored and holds live secrets
├── docs/               # docs/architecture-v4.md (Chinese baseline) + ADRs
└── artifacts/          # run outputs (gitignored); profiles/ holds the Chrome profile lock
```

## WHERE TO LOOK
| Task | Location | Notes |
|---|---|---|
| Add/change a CLI command | src/agentcal/cli.py | `build_parser` + `cmd_*`; `_runtime` wires the loop |
| Change task/run states | src/agentcal/fsm.py | transitions, leases, backoff, reconcile |
| Calendar intake (sync -> commands) | src/agentcal/poller.py, src/agentcal/calendar_client.py | syncToken/410, 429, self-write filter |
| Calendar projection (colorId + log) | src/agentcal/projector.py, src/agentcal/envelope.py | 3-region description, If-Match/412 retry |
| Auth and policy gates | src/agentcal/policy.py, src/agentcal/auth.py | fail closed; per-agent domains; OAuth PKCE |
| Add an agent | src/agentcal/agents/ | `REGISTRY` + `NEEDS_PROFILE`; one module per agent |
| Telegram commands/callbacks | src/agentcal/telegram.py | allowlist checked before every command |
| Notification outbox | src/agentcal/notifier.py | per-chat rate limit and backoff |
| Execute work in a subprocess | src/agentcal/executor.py, src/agentcal/runner/rpc.py | Step/AgentError contract |
| Browser automation | src/agentcal/browser/ | CDP attach, profile lock, humanizer, grounding |
| Schema | src/agentcal/migrations/, src/agentcal/db.py | append-only numbered files |
| Test fakes and fixtures | tests/helpers.py | FakeClock, FakeCalendar, FakeAPI |

## CODE MAP
AST-derived symbol index; Refs = lexical occurrences across src + tests, not LSP references.
| Symbol | Type | Location | Refs | Role |
|---|---|---|---|---|
| AgentError | class | src/agentcal/runner/protocol.py | 80 | error_class taxonomy consumed by the FSM |
| Step | dataclass | src/agentcal/runner/protocol.py | 42 | one agent action |
| AppConfig | frozen dataclass | src/agentcal/config.py | 30 | parsed TOML |
| Fsm | class | src/agentcal/fsm.py | 30 | state machine; only state writer |
| TokenSet | dataclass | src/agentcal/auth.py | 19 | OAuth token record |
| Vault | class | src/agentcal/vault.py | 16 | AES-GCM intent sealing |
| LocalRunner | class | src/agentcal/runner/rpc.py | 15 | in-process runner for tests |
| XPublisherAgent | class | src/agentcal/agents/x_publisher.py | 15 | declarative site-flow agent |
| Target | dataclass | src/agentcal/browser/grounding.py | 14 | a11y / selector / point |
| migrate | function | src/agentcal/db.py | 13 | apply numbered SQL once |

## CONVENTIONS
- Timestamps: UTC ISO-8601 seconds with `Z` (`clock.iso`). Core modules accept an injectable `clock` callable; tests use `FakeClock`.
- DB: `sqlite3` with `isolation_level=None`, WAL, busy_timeout 5s. Multi-statement writes go through `db.tx` (`BEGIN IMMEDIATE` + rollback).
- Task state changes only through `Fsm`. Task creation goes commands -> `Scheduler.apply_commands`, unique on `idem_key`.
- Every external side effect is preceded by a claim and a hashed `publish_intent` artifact. Projections are outbox-driven and never block or roll back task progress.
- Migrations are append-only numbered files; `schema_migrations.version` marks applied ones.
- Agents are a code registry (`REGISTRY`, `NEEDS_PROFILE`) plus `[agents.<name>]` config; no runtime file drops.
- Secrets come from `secrets.get_hex/get_text`: env `AGENTCAL_*` first, macOS Keychain (service `agentcal`) second.
- Tests: stdlib `unittest`, offline, shared fakes in `tests/helpers.py`. No pytest, no conftest, no new test deps.
- Dependency direction: intake (poller/telegram) -> commands -> scheduler -> fsm -> executor -> runner/agents; browser sits below agents only.

## ANTI-PATTERNS (THIS PROJECT)
- NEVER read calendar `colorId` (or any projected field) to decide task state; projection is one-way.
- NEVER reuse `context.pages[0]` on the shared CDP instance; pick a tab by host or open one (ADR 0003).
- NEVER edit or renumber an applied migration; add `000N_*.sql`.
- NEVER put payloads, URLs, accounts, or tokens in the calendar description; only `payload_ref` + signature.
- NEVER write task/run state outside `Fsm`, and never let projection or notification failure change task progress.
- NEVER import playwright at module scope; it is optional and only browser agents need it.
- NEVER add a web UI, Redis, or agent-to-agent handoff: explicitly out of MVP scope in architecture v4.

## UNIQUE STYLES
- `except Exception:  # noqa: BLE001 - <reason>` only at loop/projection boundaries that must survive; re-raise everywhere else.
- Chinese comments and user-facing strings appear in src/agentcal/fsm.py, src/agentcal/cli.py, src/agentcal/telegram.py; keep new user-facing text consistent.
- Records and config are frozen dataclasses; no mutable module globals for runtime state.

## COMMANDS
```bash
PYTHONPATH=src:. python3 -m unittest discover -s tests -t .   # 154 tests, offline
python3 -m agentcal --config config/agentcal.toml init-db
python3 -m agentcal --config config/agentcal.toml tick --once --offline
python3 -m agentcal --config config/agentcal.toml serve --poll-timeout 20
```
Offline quickstart also needs `AGENTCAL_VAULT_HEX` and `AGENTCAL_HMAC_HEX` (32-byte hex) or Keychain entries.

## NOTES
- Python >= 3.12 is required (`tomllib`).
- README still claims "91 tests"; the suite currently runs 154 (2026-11-19).
- `config/agentcal.toml` is gitignored and contains a live OAuth client secret and calendar id; treat it as a secret, never echo or commit it.
- `docs/architecture-v4.md` is the decision-complete baseline (Chinese); ADRs 0001-0003 win where they disagree.
- Browser agents expect Chrome debug on `127.0.0.1:9222`; `playwright install chromium` is only needed for `mode = "persistent"`.
