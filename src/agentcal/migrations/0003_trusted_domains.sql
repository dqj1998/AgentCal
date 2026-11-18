-- 0003: permanently approved outbound domains (listable, revocable)
CREATE TABLE IF NOT EXISTS trusted_domains (
  domain     TEXT PRIMARY KEY,
  source     TEXT NOT NULL,        -- approval | cli | telegram
  created_at TEXT NOT NULL,
  note       TEXT
);
