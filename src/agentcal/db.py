"""SQLite connection, migrations and transaction helper."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: str | Path, *, wal: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> int:
    """Apply numbered .sql files once. Returns the number applied."""
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    applied = 0
    for path in sorted(directory.glob("*.sql")):
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError:
            continue
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in sql.split(";"):
                stmt = statement.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES(?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                (version,),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        applied += 1
    return applied


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT with rollback on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
