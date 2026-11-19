# tests

## OVERVIEW
Offline stdlib-unittest suite (score 15, no conftest, no pytest): 154 tests drive the real FSM, scheduler, executor, and projector against fake transports and a fake clock.

## WHERE TO LOOK
| Need | File |
|---|---|
| FakeClock, FakeCalendar, `make_env`, `insert_task`, signed descriptions | helpers.py |
| End-to-end tick: poll -> task -> success -> projection | test_smoke.py |
| Acceptance T5/T7/T9 and publish-intent ordering | test_acceptance_m1.py |
| Calendar edge cases (410 resync, 429, 412 retry, self-write filter) | test_poller.py |
| State machine, leases, approvals, reconcile | test_fsm.py |
| Telegram commands, callbacks, rate limiting | test_telegram.py (`FakeAPI` + `httpx.MockTransport`) |
| Browser driver math with a fake page | test_driver.py, test_browser_math.py |
| Agent flows through `LocalRunner` | test_agents.py, test_publisher.py, test_blogger.py, test_telegraph.py, test_hashnode.py |

## CONVENTIONS
- Run with `PYTHONPATH=src:. python3 -m unittest discover -s tests -t .`; no network, no Google or Telegram credentials.
- Every test gets a fresh `tempfile.mkdtemp()` database via `make_env`; migrations run for real, never mocked.
- Time is explicit: create a `FakeClock` and call `advance(...)`; never sleep or poll.
- HTTP is faked with `httpx.MockTransport(api)`; DB and filesystem are real.
- Browser code is exercised through the `Page` Protocol (see `test_driver.FakePage`) with `sleep=lambda s: None`.
- Use `LocalRunner` for agent flows and `WorkerRunner` only when the subprocess boundary itself is under test.
- Test names carry acceptance IDs (T1-T10) where a requirement maps to one.

## ANTI-PATTERNS
- NEVER hit the network, real Keychain, or `security` CLI; use `MemoryTokenStore` or fakes.
- NEVER loosen helpers or assertions to make a test pass; fix the production contract instead.
- NEVER introduce time-based sleeps, polling waits, or flaky ordering.
- NEVER assert on prose or doc text; assert machine-consumed values (`error_class`, `state`, callback ids, artifact kinds).
- NEVER add pytest, async test frameworks, or new test dependencies.
