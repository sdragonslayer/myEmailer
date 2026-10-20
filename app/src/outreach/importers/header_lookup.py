"""Header-only last-communication lookup. Never fetches a body.

Two searches per contact, one per direction, each returning only the newest match:
  inbound : {from:a from:b}                 -> the latest message *they* sent
  outbound: from:me {to:a cc:a to:b cc:b}   -> the latest message *I* sent them
Each hit is verified against its headers (Gmail search is fuzzier than exact address match) before
it counts. From the pair we get: last contact date + direction, whether the relationship is
two-way, and whether my last email is unanswered (my latest is newer than their latest).

Stored mail is used instead of a search only when it is fresh: imported/synced within the last
hour, so nothing newer can be missing.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..comms import latest_direct, refresh_status
from ..config import Config
from ..db import get_state, now_iso, now_ms
from ..gmail.client import GmailClient, GmailUnavailable
from ..gmail.store import direction_of, link_contact, my_addresses, participants, store_message

LOCAL_FRESH_MS = 3_600_000


@dataclass
class LookupReport:
    contacts: int = 0
    directions_from_local: int = 0
    directions_from_gmail: int = 0
    found: int = 0
    not_found: int = 0
    no_email: int = 0
    unverified_hits: int = 0          # search hit whose headers didn't actually involve the contact
    api_calls: int = 0
    errors: list[str] = field(default_factory=list)


def _emails_of(conn, cid: int) -> list[str]:
    return [r[0] for r in conn.execute("SELECT email FROM contact_emails WHERE contact_id = ?", (cid,))]


def query_for(direction: str, emails: list[str]) -> str:
    if direction == "inbound":
        return "{" + " ".join(f"from:{e}" for e in emails) + "} -in:chats"
    return "from:me {" + " ".join(f"to:{e} cc:{e}" for e in emails) + "} -in:chats"


def _fresh_local_since(conn, now: int) -> int | None:
    """Window start if stored mail is current enough to be authoritative, else None."""
    initial = get_state(conn, "initial_cutoff_ms")
    marks = [int(v) for v in (get_state(conn, "last_window_import_ms"), get_state(conn, "last_sync_ms")) if v]
    if initial is None or not marks or now - max(marks) > LOCAL_FRESH_MS:
        return None
    return int(initial)


def _gmail_latest(conn, cfg, client: GmailClient, cid: int, emails: list[str], direction: str,
                  rep: LookupReport) -> tuple[int, str | None, str, str] | None:
    refs = client.list_message_ids(query_for(direction, emails), max_results=1)
    rep.api_calls += 1
    if not refs:
        return None
    head = client.get_message_headers(refs[0]["id"])
    rep.api_calls += 1
    mine = my_addresses(conn, cfg)
    actual = direction_of(head, mine)
    emails_set = set(emails)
    roles = [role for role, _, addr in participants(head) if addr in emails_set]
    ok = (actual == "inbound" and "from" in roles) if direction == "inbound" else \
         (actual == "outbound" and any(r in ("to", "cc") for r in roles))
    email_id, _ = store_message(conn, head, direction=actual, body=None, floor_ms=None, fetched_at_ms=now_ms())
    for r in roles:
        link_contact(conn, email_id, cid, r)
    if not ok:
        rep.unverified_hits += 1
        return None
    if direction == "inbound":
        _record_my_earlier_messages_in_thread(conn, client, cid, emails_set, head, mine, rep)
    return head.internal_date, head.headers.get("subject"), head.id, head.thread_id


def _record_my_earlier_messages_in_thread(conn, client: GmailClient, cid: int, emails_set: set[str],
                                          inbound, mine: set[str], rep: LookupReport) -> None:
    """Was their latest message a reply to me? Check the thread's headers (never bodies) for an
    earlier message from me to them, and store it -- that's what makes status 'replied' knowable."""
    rep.api_calls += 1
    for h in client.get_thread_headers(inbound.thread_id):
        if h.internal_date >= inbound.internal_date or direction_of(h, mine) != "outbound":
            continue
        roles = [role for role, _, addr in participants(h) if addr in emails_set and role in ("to", "cc")]
        if roles:
            email_id, _ = store_message(conn, h, direction="outbound", body=None, floor_ms=None,
                                        fetched_at_ms=now_ms())
            for r in roles:
                link_contact(conn, email_id, cid, r)


def stored_last(conn: sqlite3.Connection, cid: int) -> dict:
    """Best known answer without Gmail: the stored lookup result, else stored direct mail. Marked stale."""
    lc = conn.execute("SELECT * FROM last_communication WHERE contact_id = ?", (cid,)).fetchone()
    if lc and lc["found"]:
        return {"contact_id": cid, "found": True, "direction": lc["direction"], "date_ms": lc["date_ms"],
                "subject": lc["subject"], "two_way": bool(lc["last_in_ms"] and lc["last_out_ms"]),
                "my_last_unanswered": bool(lc["my_last_unanswered"]), "stale": True,
                "checked_at": lc["checked_at"]}
    rows = [r for r in (latest_direct(conn, cid, d) for d in ("inbound", "outbound")) if r]
    if not rows:
        return {"contact_id": cid, "found": False, "stale": True, "reason": "gmail unavailable; nothing stored"}
    newest = max(rows, key=lambda r: r["internal_date"])
    return {"contact_id": cid, "found": True, "direction": newest["direction"],
            "date_ms": newest["internal_date"], "subject": newest["subject"], "two_way": len(rows) == 2,
            "my_last_unanswered": newest["direction"] == "outbound", "stale": True}


def lookup_one(conn: sqlite3.Connection, cfg: Config, client: GmailClient | None, cid: int,
               *, rep: LookupReport | None = None, now: int | None = None) -> dict:
    rep = rep or LookupReport()
    emails = _emails_of(conn, cid)
    if not emails:
        rep.no_email += 1
        return {"contact_id": cid, "found": False, "reason": "no email address"}
    now = now or now_ms()
    since = _fresh_local_since(conn, now)
    latest: dict[str, tuple | None] = {}
    for direction in ("inbound", "outbound"):
        row = latest_direct(conn, cid, direction, since) if since is not None else None
        if row:
            rep.directions_from_local += 1
            latest[direction] = (row["internal_date"], row["subject"], row["gmail_id"], row["thread_id"])
        elif client is None:
            return stored_last(conn, cid)
        else:
            rep.directions_from_gmail += 1
            latest[direction] = _gmail_latest(conn, cfg, client, cid, emails, direction, rep)

    li, lo = latest["inbound"], latest["outbound"]
    newest_dir = None
    if li or lo:
        newest_dir = "inbound" if (li and (not lo or li[0] >= lo[0])) else "outbound"
    newest = latest[newest_dir] if newest_dir else None
    conn.execute(
        "INSERT INTO last_communication(contact_id, gmail_id, thread_id, date_ms, direction, subject,"
        " last_in_ms, last_in_subject, last_out_ms, last_out_subject, my_last_unanswered, found,"
        " checked_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'header_lookup')"
        " ON CONFLICT(contact_id) DO UPDATE SET gmail_id=excluded.gmail_id, thread_id=excluded.thread_id,"
        " date_ms=excluded.date_ms, direction=excluded.direction, subject=excluded.subject,"
        " last_in_ms=excluded.last_in_ms, last_in_subject=excluded.last_in_subject,"
        " last_out_ms=excluded.last_out_ms, last_out_subject=excluded.last_out_subject,"
        " my_last_unanswered=excluded.my_last_unanswered, found=excluded.found,"
        " checked_at=excluded.checked_at, source=excluded.source",
        (cid, newest and newest[2], newest and newest[3], newest and newest[0], newest_dir,
         newest and newest[1], li and li[0], li and li[1], lo and lo[0], lo and lo[1],
         int(newest_dir == "outbound"), int(newest is not None), now_iso()))
    refresh_status(conn, cid)
    if newest:
        rep.found += 1
    else:
        rep.not_found += 1
    return {"contact_id": cid, "found": newest is not None, "direction": newest_dir,
            "two_way": bool(li and lo), "my_last_unanswered": newest_dir == "outbound"}


def lookup_all(conn: sqlite3.Connection, cfg: Config, client: GmailClient, *,
               only_missing: bool = True, now: int | None = None) -> LookupReport:
    rep = LookupReport()
    sql = "SELECT c.id FROM contacts c WHERE c.merged_into_id IS NULL"
    if only_missing:
        sql += " AND c.id NOT IN (SELECT contact_id FROM last_communication)"
    for (cid,) in conn.execute(sql + " ORDER BY c.id").fetchall():
        rep.contacts += 1
        try:
            lookup_one(conn, cfg, client, cid, rep=rep, now=now)
        except GmailUnavailable:
            raise
        except Exception as e:  # noqa: BLE001 - one bad contact shouldn't stop the run; re-run retries it
            rep.errors.append(f"contact {cid}: {type(e).__name__}: {e}")
    return rep
