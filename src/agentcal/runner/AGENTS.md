# src/agentcal/runner

## OVERVIEW
Subprocess boundary (score 8, distinct domain): Executor spawns `python -m agentcal.runner.worker` and consumes JSON lines; `LocalRunner` replays the same protocol in-process for tests.

## WHERE TO LOOK
- `protocol.py`: `Step`, `Agent`, `AgentError(error_class, message)` - the contract every agent implements.
- `worker.py`: reads one JSON request from stdin, emits `steps` / `step` / `done`, always answers.
- `rpc.py`: `WorkerRunner` (spawn, pump stdout on a thread, timeout, kill), `RunnerTimeout`, `RunnerError`, `LocalRunner`.

## EVENT CONTRACT
- `{"event":"steps","steps":[{"kind":...,"target":...}]}`
- `{"event":"step","idx":N,"state":"started"|"done"|"failed",...}` (`done` carries `output_ref`, `failed` carries `error_class`)
- `{"event":"done","ok":bool,"error_class":...,"detail":...}`
- Executor `_consume` is the authoritative consumer; changing a shape requires updating it and both runners.

## CONVENTIONS
- stdout carries protocol JSON only; diagnostics belong on stderr.
- Closed error vocabulary: `transient | policy | human_required | unknown_side_effect`.
- A child that exits without `done` raises `RunnerError`; silence for `timeout_s` (default 120 s) raises `RunnerTimeout` and becomes a transient retry.
- The parent kills and waits for the child in `finally`; children never outlive the tick.
- `LocalRunner` must mirror `WorkerRunner` exactly; acceptance tests depend on that parity.
- `worker.py` itself performs no DB writes; agents may open `settings.db_path` for policy reads (`trusted_domains`) and rate-limit writes (`rate_events`).

## ANTI-PATTERNS
- NEVER print non-JSON to stdout or leave a request unanswered.
- NEVER change an event shape in one runner only.
- NEVER swallow agent exceptions without emitting `failed` + `done`.
- NEVER put task/run state transitions in the worker or an agent; only the parent-side Executor/FSM writes state.
