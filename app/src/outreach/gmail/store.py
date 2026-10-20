"""Persisting Gmail messages and computing the body floor."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..config import Config
from ..db import get_state, set_state
from ..normalize import canonical_email, parse_addresses
from .client import Headers


def body_floor_ms(conn: sqlite3.Connection, cfg: Config, now_ms: int) -> int:
    """Earliest internalDate that may have its body fetched right now.

    Strict window by default. With rolling sync on, extends back to the last sync so the stored
    window only ever grows forward without gaps -- but never before the very first import's cutoff.
    """
    initial = get_state(conn, "initial_cutoff_ms")
    if initial is None:
        initial = now_ms - cfg.body_window_ms
        set_state(conn, "initial_cutoff_ms", initial)
    floor = now_ms - cfg.body_window_ms
    last_sync = get_state(conn, "last_sync_ms")
    if cfg.rolling_sync_enabled and last_sync is not None:
        floor = min(floor, int(last_sync))
    return max(floor, int(initial))


def my_addresses(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    mine = {canonical_email(e) for e in cfg.my_emails}
    prof = get_state(conn, "profile_email")
    if prof:
        mine.add(canonical_email(prof))
    mine |= {canonical_email(a) for a in json.loads(get_state(conn, "send_as_aliases") or "[]")}
    return mine


def direction_of(h: Headers, mine: set[str]) -> str:
    sender = parse_addresses(h.headers.get("from"))
    if "SENT" in h.label_ids or (sender and sender[0][1] in mine):
        return "outbound"
    return "inbound"


def participants(h: Headers) -> list[tuple[str, str, str]]:
    """[(role, display_name, canonical_email)]"""
    out = []
    for role, key in (("from", "from"), ("to", "to"), ("cc", "cc")):
        for name, addr in parse_addresses(h.headers.get(key)):
            out.append((role, name, addr))
    return out


def store_message(conn: sqlite3.Connection, h: Headers, *, direction: str, body: str | None,
                  floor_ms: int | None, fetched_at_ms: int) -> tuple[int, bool]:
    """Insert (or upgrade metadata->full) one message. Returns (email_id, created)."""
    sender = parse_addresses(h.headers.get("from"))
    to = [a for _, a in parse_addresses(h.headers.get("to"))]
    cc = [a for _, a in parse_addresses(h.headers.get("cc"))]
    row = conn.execute("SELECT id, fetch_format FROM emails WHERE gmail_id = ?", (h.id,)).fetchone()
    fmt = "full" if body is not None else "metadata"
    if row:
        if fmt == "full" and row["fetch_format"] == "metadata":
            conn.execute("UPDATE emails SET fetch_format = 'full', body = ?, body_floor_ms = ?,"
                         " fetched_at_ms = ? WHERE id = ?", (body, floor_ms, fetched_at_ms, row["id"]))
        return row["id"], False
    cur = conn.execute(
        "INSERT INTO emails(gmail_id, thread_id, internal_date, from_addr, from_name, to_addrs, cc_addrs,"
        " subject, direction, fetch_format, body, body_floor_ms, fetched_at_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (h.id, h.thread_id, h.internal_date, sender[0][1] if sender else None,
         sender[0][0] if sender else None, json.dumps(to), json.dumps(cc), h.headers.get("subject"),
         direction, fmt, body, floor_ms if body is not None else None, fetched_at_ms),
    )
    return cur.lastrowid, True


def link_contact(conn: sqlite3.Connection, email_id: int, contact_id: int, role: str) -> None:
    conn.execute("INSERT OR IGNORE INTO email_contacts(email_id, contact_id, role) VALUES (?, ?, ?)",
                 (email_id, contact_id, role))


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds")


def header_line(h: Headers, direction: str) -> str:
    hd = h.headers
    return (f"{ms_to_iso(h.internal_date)} | {direction} | from: {hd.get('from', '')} | "
            f"to: {hd.get('to', '')} | cc: {hd.get('cc', '')} | subject: {hd.get('subject', '')} | "
            f"gmail_id: {h.id} | thread: {h.thread_id}")
