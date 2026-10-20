"""Direct communication between me and a contact, derived from stored mail + header lookups.

A message counts as *direct* only if:
  inbound  -- they sent it (role 'from' on an inbound message), or
  outbound -- I sent it with them in To/Cc.
Being cc'd on someone else's mail to me is not communication between us, so it never feeds
direction, reciprocity, "unanswered" or pipeline status.
"""
from __future__ import annotations

import sqlite3

DIRECT_SQL = ("((e.direction = 'inbound' AND ec.role = 'from') OR "
              "(e.direction = 'outbound' AND ec.role IN ('to','cc')))")

STATUS_RANK = {"to_contact": 0, "dormant": 1, "contacted": 2, "replied": 3, "meeting": 4}


def direct_stats(conn: sqlite3.Connection) -> dict[int, dict[str, tuple[int, int]]]:
    """{contact_id: {'inbound': (count, last_ms), 'outbound': (count, last_ms)}} over stored mail."""
    out: dict[int, dict[str, tuple[int, int]]] = {}
    for r in conn.execute(
            f"SELECT ec.contact_id, e.direction, COUNT(DISTINCT e.id) n, MAX(e.internal_date) last "
            f"FROM email_contacts ec JOIN emails e ON e.id = ec.email_id WHERE {DIRECT_SQL} "
            f"GROUP BY ec.contact_id, e.direction"):
        out.setdefault(r["contact_id"], {})[r["direction"]] = (r["n"], r["last"])
    return out


def latest_direct(conn: sqlite3.Connection, cid: int, direction: str,
                  since_ms: int | None = None) -> sqlite3.Row | None:
    sql = (f"SELECT e.* FROM emails e JOIN email_contacts ec ON ec.email_id = e.id "
           f"WHERE ec.contact_id = ? AND e.direction = ? AND {DIRECT_SQL}")
    params: list = [cid, direction]
    if since_ms is not None:
        sql += " AND e.internal_date >= ?"
        params.append(since_ms)
    return conn.execute(sql + " ORDER BY e.internal_date DESC LIMIT 1", params).fetchone()


def is_direct(direction: str, role: str) -> bool:
    return (direction == "inbound" and role == "from") or (direction == "outbound" and role in ("to", "cc"))


def advance_status(conn: sqlite3.Connection, cid: int, status: str) -> None:
    """Move a contact forward in the pipeline; never backwards automatically."""
    cur = conn.execute("SELECT status FROM contacts WHERE id = ?", (cid,)).fetchone()
    if cur and STATUS_RANK[status] > STATUS_RANK[cur[0]]:
        conn.execute("UPDATE contacts SET status = ? WHERE id = ?", (status, cid))


def refresh_status(conn: sqlite3.Connection, cid: int) -> None:
    """contacted: I have written to them. replied: they wrote after one of my messages."""
    stats = direct_stats_for(conn, cid)
    lc = conn.execute("SELECT last_in_ms, last_out_ms FROM last_communication WHERE contact_id = ?",
                      (cid,)).fetchone()
    first_out = conn.execute(
        f"SELECT MIN(e.internal_date) FROM emails e JOIN email_contacts ec ON ec.email_id = e.id "
        f"WHERE ec.contact_id = ? AND e.direction = 'outbound' AND {DIRECT_SQL}", (cid,)).fetchone()[0]
    last_in = max([x for x in (stats.get("inbound", (0, None))[1], lc and lc["last_in_ms"]) if x], default=None)
    last_out = max([x for x in (stats.get("outbound", (0, None))[1], lc and lc["last_out_ms"]) if x], default=None)
    earliest_out = min([x for x in (first_out, last_out) if x], default=None)
    if earliest_out is not None:
        advance_status(conn, cid, "contacted")
    if last_in is not None and earliest_out is not None and last_in > earliest_out:
        advance_status(conn, cid, "replied")


def direct_stats_for(conn: sqlite3.Connection, cid: int) -> dict[str, tuple[int, int]]:
    out = {}
    for r in conn.execute(
            f"SELECT e.direction, COUNT(DISTINCT e.id) n, MAX(e.internal_date) last "
            f"FROM email_contacts ec JOIN emails e ON e.id = ec.email_id "
            f"WHERE ec.contact_id = ? AND {DIRECT_SQL} GROUP BY e.direction", (cid,)):
        out[r["direction"]] = (r["n"], r["last"])
    return out
