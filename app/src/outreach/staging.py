"""Review queue. Every staged change links to a capture; applying one records where it landed."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import now_iso, tx


def stage(conn: sqlite3.Connection, *, capture_id: int, kind: str, payload: dict,
          match_status: str, confidence: str, target_table: str | None = None,
          target_id: int | None = None, field: str | None = None, old_value: Any = None,
          new_value: Any = None, candidates: list | None = None, reason: str | None = None,
          date_phrase: str | None = None, resolved_date: str | None = None) -> int:
    if capture_id is None:
        raise ValueError("staged changes must link to a capture")
    if target_id is not None and field is not None:
        # The same proposal already waiting, or already rejected by me: don't ask again.
        dup = conn.execute(
            "SELECT id FROM staged_changes WHERE kind = ? AND target_id = ? AND field = ? AND new_value IS ?"
            " AND status IN ('pending','rejected')",
            (kind, target_id, field, None if new_value is None else str(new_value))).fetchone()
        if dup:
            return dup[0]
    cur = conn.execute(
        "INSERT INTO staged_changes(capture_id, kind, payload_json, target_table, target_id, field,"
        " old_value, new_value, match_status, candidates_json, confidence, reason, date_phrase,"
        " resolved_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (capture_id, kind, json.dumps(payload, default=str), target_table, target_id, field,
         None if old_value is None else str(old_value), None if new_value is None else str(new_value),
         match_status, json.dumps(candidates, default=str) if candidates is not None else None,
         confidence, reason, date_phrase, resolved_date, now_iso()),
    )
    return cur.lastrowid


def list_changes(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    rows = conn.execute("SELECT * FROM staged_changes WHERE status = ? ORDER BY id", (status,)).fetchall()
    return [_row(r) for r in rows]


def get_change(conn: sqlite3.Connection, change_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM staged_changes WHERE id = ?", (change_id,)).fetchone()
    return _row(r) if r else None


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d.pop("payload_json"))
    d["candidates"] = json.loads(d.pop("candidates_json")) if d.get("candidates_json") else None
    return d


class ReviewError(Exception):
    pass


def reject(conn: sqlite3.Connection, change_id: int) -> None:
    _require_pending(conn, change_id)
    conn.execute("UPDATE staged_changes SET status = 'rejected', decided_at = ? WHERE id = ?",
                 (now_iso(), change_id))


def approve(conn: sqlite3.Connection, change_id: int, *, choice: int | None = None,
            edits: dict | None = None, auto: bool = False) -> str:
    """Apply a staged change. `choice` picks a candidate for ambiguous merges; `edits` overrides payload."""
    ch = _require_pending(conn, change_id)
    payload = {**ch["payload"], **(edits or {})}
    handler = _APPLY.get(ch["kind"])
    if handler is None:
        raise ReviewError(f"no handler for kind {ch['kind']!r}")
    if ch["match_status"] == "ambiguous" and choice is None and ch["kind"] != "merge_contacts":
        raise ReviewError("ambiguous change: pick a candidate first")
    with tx(conn):
        ref = handler(conn, ch, payload, choice)
        status = "auto_approved" if auto else ("edited" if edits else "approved")
        conn.execute("UPDATE staged_changes SET status = ?, decided_at = ?, applied_ref = ?,"
                     " payload_json = ? WHERE id = ?",
                     (status, now_iso(), ref, json.dumps(payload, default=str), change_id))
    return ref


def _require_pending(conn, change_id: int) -> dict:
    ch = get_change(conn, change_id)
    if ch is None:
        raise ReviewError(f"no staged change {change_id}")
    if ch["status"] != "pending":
        raise ReviewError(f"change {change_id} is already {ch['status']}")
    return ch


# ---------------------------------------------------------------------- handlers
def _apply_merge(conn, ch, payload, choice) -> str:
    from .contacts import merge_contacts
    cands = payload["candidate_ids"]
    if choice is None:
        if len(cands) != 1:
            raise ReviewError("several candidates: pick which existing contact this is")
        choice = cands[0]
    if choice not in cands:
        raise ReviewError(f"{choice} is not one of the candidates {cands}")
    merge_contacts(conn, keep_id=choice, drop_id=payload["new_contact_id"])
    return f"contacts:{choice}"


_CONTACT_FIELDS = {"display_name", "org_id", "title", "linkedin_url", "location", "notes",
                   "recipient_type", "status"}


def _apply_update_contact(conn, ch, payload, choice) -> str:
    f = payload["field"]
    if f not in _CONTACT_FIELDS:
        raise ReviewError(f"field {f!r} cannot be updated")
    conn.execute(f"UPDATE contacts SET {f} = ?, updated_at = ? WHERE id = ?",
                 (payload["value"], now_iso(), payload["contact_id"]))
    return f"contacts:{payload['contact_id']}"


_APPLY = {
    "merge_contacts": _apply_merge,
    "update_contact": _apply_update_contact,
}


def register(kind: str, handler) -> None:
    _APPLY[kind] = handler
