"""Reconcile the audit log against the database.

Checks:
  1. Every stored body has a format=full audit entry for that message ID.
  2. No format=full audit entry exists for a message whose internalDate is below the floor
     that was in force for that fetch (recorded in the entry's params.body_floor_ms).
  3. Every stored body satisfies internal_date >= body_floor_ms >= initial cutoff.
  4. Every stored message (headers-only rows too) has a logged fetch.
  5. No forbidden call was attempted (any entry with error == 'forbidden' is reported).
  6. Counts: messages stored vs. distinct IDs fetched per format; fetched-but-not-stored IDs listed.
"""
from __future__ import annotations

import sqlite3

from .audit import AuditLog
from .db import get_state


def verify(conn: sqlite3.Connection, audit: AuditLog) -> dict:
    entries = audit.entries()
    full_fetches: dict[str, int | None] = {}
    meta_ids: set[str] = set()
    forbidden = []
    errors = []
    for e in entries:
        if e["method"] == "messages.get" and e.get("ok"):
            for mid in e.get("ids", []):
                if e.get("format") == "full":
                    full_fetches[mid] = e.get("params", {}).get("body_floor_ms")
                else:
                    meta_ids.add(mid)
        if e.get("error") == "forbidden":
            forbidden.append(e)
        elif not e.get("ok"):
            errors.append(e)

    problems: list[str] = []
    initial = get_state(conn, "initial_cutoff_ms")
    bodies = conn.execute("SELECT gmail_id, internal_date, body_floor_ms FROM emails WHERE body IS NOT NULL").fetchall()
    for r in bodies:
        if r["gmail_id"] not in full_fetches:
            problems.append(f"body stored for {r['gmail_id']} with no full-fetch audit entry")
        if r["body_floor_ms"] is None or r["internal_date"] < r["body_floor_ms"]:
            problems.append(f"body for {r['gmail_id']} predates its floor")
        if initial and r["body_floor_ms"] is not None and r["body_floor_ms"] < int(initial):
            problems.append(f"floor for {r['gmail_id']} is before the initial cutoff")

    dates = {r["gmail_id"]: r["internal_date"] for r in conn.execute("SELECT gmail_id, internal_date FROM emails")}
    for mid, floor in full_fetches.items():
        if floor is None:
            problems.append(f"full fetch of {mid} has no recorded floor")
        elif mid in dates and dates[mid] < floor:
            problems.append(f"full fetch of {mid} although it is older than the floor")

    # Everything stored (headers-only rows too) must trace back to a logged fetch.
    fetched_any = meta_ids | set(full_fetches)
    for mid in dates:
        if mid not in fetched_any:
            problems.append(f"message {mid} is stored but no fetch of it was logged")
    # Fetched-but-not-stored is legitimate (e.g. a run interrupted before commit); reported, not a problem.
    fetched_not_stored = sorted(fetched_any - dates.keys())

    for e in forbidden:
        problems.append(f"forbidden call attempted: {e['method']} at {e['ts']}")

    oldest_body = conn.execute("SELECT MIN(internal_date) FROM emails WHERE body IS NOT NULL").fetchone()[0]
    return {
        "ok": not problems,
        "problems": problems,
        "audit_entries": len(entries),
        "full_fetch_ids": len(full_fetches),
        "metadata_fetch_ids": len(meta_ids),
        "stored_with_body": len(bodies),
        "stored_headers_only": conn.execute("SELECT COUNT(*) FROM emails WHERE body IS NULL").fetchone()[0],
        "initial_cutoff_ms": int(initial) if initial else None,
        "oldest_body_internal_date_ms": oldest_body,
        "fetched_not_stored": fetched_not_stored,
        "api_errors": [f"{e['method']}: {e.get('error')}" for e in errors],
    }
