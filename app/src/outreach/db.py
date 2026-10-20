"""SQLite access. One connection per process; rows come back as sqlite3.Row."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; use tx() for atomic blocks
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("outreach").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    if conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))


@contextmanager
def tx(conn: sqlite3.Connection):
    """Atomic block. Nested use is flattened into the outermost transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_state(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn: sqlite3.Connection, key: str, value: str | int) -> None:
    conn.execute(
        "INSERT INTO sync_state(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
