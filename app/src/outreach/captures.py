"""Captures: permanent raw text. Everything structured links back to one of these."""
from __future__ import annotations

import hashlib
import sqlite3

from .db import now_iso


def content_hash(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}\0{text}".encode("utf-8")).hexdigest()


def add_capture(conn: sqlite3.Connection, raw_text: str, source: str, *,
                source_ref: str | None = None, captured_at: str | None = None,
                status: str = "pending") -> tuple[int, bool]:
    """Insert a capture. Returns (capture_id, created). Identical (source, text) is de-duplicated."""
    if not raw_text.strip():
        raise ValueError("capture text is empty")
    h = content_hash(source, raw_text)
    row = conn.execute("SELECT id FROM captures WHERE content_hash = ?", (h,)).fetchone()
    if row:
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO captures(raw_text, source, source_ref, content_hash, captured_at, status)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (raw_text, source, source_ref, h, captured_at or now_iso(), status),
    )
    return cur.lastrowid, True


def get_capture(conn: sqlite3.Connection, capture_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()


def mark_processed(conn: sqlite3.Connection, capture_id: int) -> None:
    conn.execute("UPDATE captures SET status = 'processed', processed_at = ? WHERE id = ?",
                 (now_iso(), capture_id))
