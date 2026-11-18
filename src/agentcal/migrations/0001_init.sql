-- 0001_init: truth-source schema (see design v4.0 §3)
CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  idem_key       TEXT NOT NULL UNIQUE,
  source         TEXT NOT NULL,
  actor_id       TEXT NOT NULL,
  title          TEXT NOT NULL,
  intent_enc     BLOB NOT NULL,
  agent          TEXT,
  confidence     REAL,
  state          TEXT NOT NULL,
  attempt        INTEGER NOT NULL DEFAULT 0,
  not_before     TEXT NOT NULL,
  due_at         TEXT NOT NULL,
  policy_snap    TEXT NOT NULL,
  calendar_id    TEXT,
  event_id       TEXT,
  event_etag     TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  id               TEXT PRIMARY KEY,
  task_id          TEXT NOT NULL REFERENCES tasks(id),
  attempt_no       INTEGER NOT NULL,
  state            TEXT NOT NULL,
  lease_owner      TEXT,
  lease_expires_at TEXT,
  started_at       TEXT,
  ended_at         TEXT,
  exit_reason      TEXT,
  UNIQUE(task_id, attempt_no)
);

-- 不变式：一任务至多一个活跃 run（含 blocked，避免人工介入期间重入）
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_active_run ON runs(task_id)
  WHERE state IN ('leased','running','blocked');
CREATE INDEX IF NOT EXISTS ix_tasks_due ON tasks(state, not_before);
CREATE INDEX IF NOT EXISTS ix_runs_lease ON runs(state, lease_expires_at);

CREATE TABLE IF NOT EXISTS steps (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL REFERENCES runs(id),
  idx         INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  target      TEXT,
  input_ref   TEXT,
  output_ref  TEXT,
  state       TEXT NOT NULL,
  error_class TEXT,
  started_at  TEXT,
  ended_at    TEXT,
  UNIQUE(run_id, idx)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id         TEXT PRIMARY KEY,
  run_id     TEXT,
  kind       TEXT NOT NULL,
  path       TEXT NOT NULL,
  bytes      INTEGER,
  sha256     TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id                TEXT PRIMARY KEY,
  task_id           TEXT,
  run_id            TEXT,
  reason            TEXT NOT NULL,
  requested_at      TEXT NOT NULL,
  expires_at        TEXT NOT NULL,
  channel_chat_id   TEXT,
  channel_msg_id    TEXT,
  channel_thread_id TEXT,
  decision          TEXT,
  decided_by        TEXT,
  decided_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_task ON approvals(task_id, decision);

CREATE TABLE IF NOT EXISTS commands (
  id            TEXT PRIMARY KEY,
  idem_key      TEXT NOT NULL UNIQUE,
  source        TEXT NOT NULL,
  actor_id      TEXT NOT NULL,
  raw_ref       TEXT,
  parsed_json   TEXT,
  signature_ok  INTEGER NOT NULL,
  policy_verdict TEXT NOT NULL,
  received_at   TEXT NOT NULL,
  applied_at    TEXT
);

CREATE TABLE IF NOT EXISTS projector_state (
  kind                   TEXT PRIMARY KEY,
  cursor                 TEXT,
  last_full_sync_at      TEXT,
  consecutive_failures   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projected_events (
  event_id     TEXT PRIMARY KEY,
  updated      TEXT NOT NULL,
  projected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_outbox (
  task_id         TEXT NOT NULL,
  kind            TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error      TEXT,
  PRIMARY KEY (task_id, kind)
);

CREATE TABLE IF NOT EXISTS tg_messages (
  chat_id   TEXT,
  message_id INTEGER,
  thread_id INTEGER,
  task_id   TEXT,
  kind      TEXT,
  updated_at TEXT,
  PRIMARY KEY(chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id                TEXT PRIMARY KEY,
  run_id            TEXT,
  route             TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  cost_usd          REAL,
  latency_ms        INTEGER,
  schema_ok         INTEGER,
  created_at        TEXT NOT NULL
);
