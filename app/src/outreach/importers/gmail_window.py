"""Phase 1: import full content of mail (sent + received) inside the body window."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from ..captures import add_capture
from ..comms import is_direct, refresh_status
from ..config import Config
from ..contacts import PersonRecord, ingest_person, resolve
from ..db import now_iso, now_ms, set_state, tx
from ..gmail.client import GmailClient, OutsideBodyWindow
from ..gmail.parse import extract_text, is_bulk
from ..gmail.store import (body_floor_ms, direction_of, header_line, link_contact, ms_to_iso,
                           my_addresses, participants, store_message)
from ..normalize import looks_automated


@dataclass
class WindowReport:
    floor_ms: int = 0
    listed: int = 0
    bodies_stored: int = 0
    headers_only: int = 0          # listed but turned out older than the floor
    already_had: int = 0
    bulk_skipped_people: int = 0   # messages whose senders were not turned into contacts
    contacts_created: int = 0
    contacts_matched: int = 0
    merges: list[tuple[int, int]] = field(default_factory=list)
    staged_ids: list[int] = field(default_factory=list)
    capture_id: int | None = None


def import_window(conn: sqlite3.Connection, cfg: Config, client: GmailClient, *,
                  now: int | None = None) -> WindowReport:
    now = now or now_ms()
    profile = client.profile()
    set_state(conn, "profile_email", profile["emailAddress"].lower())
    set_state(conn, "send_as_aliases", json.dumps(client.alias_addresses()))
    if profile.get("historyId"):
        set_state(conn, "last_history_id", profile["historyId"])
    mine = my_addresses(conn, cfg)
    floor = body_floor_ms(conn, cfg, now)
    rep = WindowReport(floor_ms=floor)

    # Gmail's after: takes epoch seconds. The query narrows the list; the per-message guard decides.
    refs = client.list_message_ids(f"after:{floor // 1000} -in:chats")
    rep.listed = len(refs)

    fetched = []  # (headers, body | None)
    for ref in refs:
        if conn.execute("SELECT 1 FROM emails WHERE gmail_id = ? AND fetch_format = 'full'",
                        (ref["id"],)).fetchone():
            rep.already_had += 1
            continue
        try:
            head, payload = client.get_message_full(ref["id"], body_floor_ms=floor)
            fetched.append((head, extract_text(payload)))
        except OutsideBodyWindow as e:
            rep.headers_only += 1
            if e.headers:
                fetched.append((e.headers, None))

    if not fetched:
        set_state(conn, "last_window_import_ms", now)
        return rep

    with tx(conn):
        lines = [f"Gmail window import at {ms_to_iso(now)}; body floor {ms_to_iso(floor)} "
                 f"({len(fetched)} messages, headers only below)"]
        dirs = {h.id: direction_of(h, mine) for h, _ in fetched}
        lines += [header_line(h, dirs[h.id]) for h, _ in fetched]
        rep.capture_id, _ = add_capture(conn, "\n".join(lines), "import:gmail",
                                        source_ref=f"window:{now}", status="processed")
        touched: set[int] = set()
        for head, body in fetched:
            direction = dirs[head.id]
            email_id, _ = store_message(conn, head, direction=direction, body=body,
                                        floor_ms=floor if body is not None else None, fetched_at_ms=now)
            if body is not None:
                rep.bodies_stored += 1
            bulk = direction == "inbound" and is_bulk(head)
            if bulk:
                rep.bulk_skipped_people += 1
                continue
            for role, name, addr in participants(head):
                if addr in mine or looks_automated(addr):
                    continue
                res = ingest_person(conn, PersonRecord(display_name=name, emails=[addr], source="gmail",
                                                       source_ref=head.id),
                                    capture_id=rep.capture_id, my_emails=mine)
                if res is None:
                    continue
                if res.action == "created":
                    rep.contacts_created += 1
                else:
                    rep.contacts_matched += 1
                rep.merges += [(res.contact_id, m) for m in res.merged_ids]
                rep.staged_ids += res.staged_ids
                link_contact(conn, email_id, res.contact_id, role)
                # Only direct mail is an interaction; being cc'd on a third party's message is not.
                if is_direct(direction, role) and not conn.execute(
                        "SELECT 1 FROM interactions WHERE email_id = ? AND contact_id = ?",
                        (email_id, res.contact_id)).fetchone():
                    conn.execute(
                        "INSERT INTO interactions(contact_id, channel, direction, occurred_at, summary,"
                        " email_id, capture_id, created_at) VALUES (?, 'email', ?, ?, ?, ?, ?, ?)",
                        (res.contact_id, direction, ms_to_iso(head.internal_date),
                         head.headers.get("subject"), email_id, rep.capture_id, now_iso()))
                touched.add(res.contact_id)
        for cid in touched:
            refresh_status(conn, resolve(conn, cid))
        set_state(conn, "last_window_import_ms", now)
    return rep
