# AgentCal

A local-first, privacy-focused AI Agent platform driven purely by Google Calendar. Built for geeks who want robust multi-agent orchestration (SLMs/LLMs hybrid) without complex web UIs. Features extreme anti-bot humanized browser automation and uses Telegram as an out-of-band intervention console.

## Status: MVP (M0-M3) implemented

Design baseline: `AgentCal 平台架构设计方案 v4.0.md` in the project root (one level up, next to the v3.0 PDF).
91 tests, stdlib `unittest`, no network required for the suite.

| Area | State |
|---|---|
| SQLite truth source, numbered migrations (0001/0002) | done |
| FSM + lease + heartbeat + reconcile + unknown-side-effect guard | done |
| Calendar poller (syncToken/410/429, recurring instances) | done |
| HMAC envelope, three-region description merge, If-Match/412 retry | done |
| Projection (colorId state machine, dirty outbox) | done |
| Executor **in a subprocess** (JSON-lines protocol) | done |
| Chromium profile lock (one profile = one lock, queue on busy) | done |
| Policy: actor allowlist, **per-agent** domain allowlist, sliding-window rate gate | done |
| First real agent: `security-scan` (allow-listed HTTP fetch, findings artifact) | done |
| Telegram: /task /status /approve /deny /retry /cancel, inline approvals, 64-byte callback ids | done |
| Notification outbox with per-chat rate limiting | done |
| LLM router: two-stage classification, schema validation, self-consistency confidence | done |
| Calibration tool (`agentcal calibrate`) | done |
| Voice: Telegram voice -> ffmpeg -> VAD gate -> whisper.cpp | done |
| Humanizer math (delays, typos, Bezier+overshoot) | done |
| Vision grounding (a11y-first, DPR/scroll conversion) | done |
| OAuth login (loopback + PKCE, Keychain store, transparent refresh) | done |
| Playwright adapter + humanized session (typing plan, hover, vision fallback) | done (CDP mode needs no browser download) |
| `x-publisher` agent: declarative per-site steps (goto/click/type/verify/wait) | done |
| Built-in site specs: X, Bluesky, Medium, LinkedIn | done (Medium/LinkedIn selectors need one live calibration) |
| `telegraph` agent (HTTP API, no browser) | done |
| `blogger` agent (iframe `execCommand` body, pointer-event publish, text confirm) | done (selector calibration on first run) |
| `hashnode` agent (GraphQL) | implemented; **blocked by provider** — free API retired 2026-05-13, Pro plan required |

Domain allowlists are per agent in `[agents.<name>] domains = [...]`; an agent without that section falls back to
`[policy].allowed_domains` (empty = deny). `security-scan` and `x-publisher` therefore cannot borrow each other's scope.

A browser agent hitting a host it is not allowed to use asks a human once (`blocked_human`, reason
`new_domain:<host>`); approving writes it to `trusted_domains` **permanently**. Audit and revoke with
`agentcal domains` / `agentcal revoke-domain <host>`, or `/domains` / `/revoke <host>` in Telegram.
Config-declared domains are seeds: revoking those requires editing the config.

Browser note: mode A attaches to the existing Chrome debug instance on `127.0.0.1:9222` (shared, by decision).
A run reuses a tab already on the target host or opens its own; it never takes `pages[0]`, which on a shared
instance can belong to another tool. `playwright install chromium` is only needed for `mode = "persistent"`.

Not implemented / not verified:

- Multi-account profiles (decision D6: single profile).
- Live runs against a third-party site: the driver and agent are exercised through a fake page, not a real signed-in account.

## Quickstart (offline, no Google or Telegram needed)

```bash
export PYTHONPATH=src
export AGENTCAL_VAULT_HEX=$(python3 -c "print('11'*32)")
export AGENTCAL_HMAC_HEX=$(python3 -c "print('22'*32)")

cp config/agentcal.example.toml config/agentcal.toml
python3 -m agentcal --config config/agentcal.toml init-db
python3 -m agentcal --config config/agentcal.toml add-task "smoke test task"
python3 -m agentcal --config config/agentcal.toml tick --once --offline
python3 -m agentcal --config config/agentcal.toml status
```

A real scan against an allow-listed host:

```bash
python3 -m agentcal --config config/agentcal.toml \
  add-task "scan http://127.0.0.1:8080/" --agent security-scan
python3 -m agentcal --config config/agentcal.toml tick --once --offline
cat artifacts/run_*/scan-*.json
```

## Commands

| Command | Purpose |
|---|---|
| `init-db` | create/migrate `agentcal.db` |
| `add-task TEXT [--agent NAME] [--target URL] [--due ISO]` | enqueue a task locally |
| `tick --once [--offline] [--local] [--llm]` | one supervisor cycle (poll -> apply -> execute -> project -> notify) |
| `serve [--offline] [--llm] [--poll-timeout N]` | long-running loop |
| `status` | task states, pending approvals, outbox sizes |
| `approve/deny/retry TASK_ID` | human decisions |
| `calibrate labels.jsonl --llm` | accuracy per confidence bucket + suggested thresholds |
| `auth login/status` | Google OAuth (loopback + PKCE); token stored in Keychain |

## Environment

| Variable | Required for |
|---|---|
| `AGENTCAL_VAULT_HEX`, `AGENTCAL_HMAC_HEX` | always (or store both in Keychain, service `agentcal`) |
| `AGENTCAL_ACCESS_TOKEN` | calendar polling without OAuth (temporary; prefer `auth login`) |
| `AGENTCAL_TELEGRAM_TOKEN` | Telegram control plane |
| `AGENTCAL_LLM_BASE` | LLM router (Ollama-compatible, default `http://127.0.0.1:11434`) |
| `AGENTCAL_WHISPER_MODEL` | voice intake (requires `whisper-cli` and `ffmpeg`) |

Keychain setup:

```bash
security add-generic-password -s agentcal -a vault -w <hex-32-bytes>
security add-generic-password -s agentcal -a hmac  -w <hex-32-bytes>
```

## Layout

```
src/agentcal/
  clock.py config.py db.py migrations/         # foundation
  envelope.py policy.py vault.py secrets.py
  calendar_client.py poller.py projector.py
  fsm.py scheduler.py executor.py supervisor.py notifier.py telegram.py
  runner/     protocol.py worker.py rpc.py     # subprocess isolation
  agents/     noop.py security_scan.py         # agent registry
  llm/        router.py schemas.py calibrate.py
  browser/    profile.py humanizer.py grounding.py vision.py
  voice.py cli.py
tests/                                          # 91 tests
docs/adr/                                       # 0001 truth source, 0002 envelope, 0003 CDP
```

## Tests

```bash
PYTHONPATH=src:. python3 -m unittest discover -s tests -t .
```

Acceptance mapping: T1 `test_poller`, T2 `test_fsm`, T3 `test_projection`, T4 `test_poller`,
T5/T7/T9 `test_acceptance_m1`, T6 `test_missed`, T8 `test_llm_router`, T10 `test_telegram`.
