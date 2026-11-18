-- 0002_mvp: rate limiting, telegram callbacks, notification outbox
CREATE TABLE IF NOT EXISTS rate_events (
  id     TEXT PRIMARY KEY,
  domain TEXT NOT NULL,
  at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rate_domain_at ON rate_events(domain, at);

CREATE TABLE IF NOT EXISTS tg_callbacks (
  short_id   TEXT PRIMARY KEY,
  task_id    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_state (
  kind   TEXT PRIMARY KEY,
  offset INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notifications (
  id              TEXT PRIMARY KEY,
  task_id         TEXT,
  kind            TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT NOT NULL,
  last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_notifications_due ON notifications(next_attempt_at);
