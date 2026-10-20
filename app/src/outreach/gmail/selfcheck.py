"""Verify, against the real mailbox, the Gmail search behaviour the importers rely on.

Tests run against a fake; these checks confirm the real API agrees. Headers only, ~15 calls.
  1. newest_first   -- messages.list returns newest first (lookups take only the first hit)
  2. from_me        -- `from:me` returns only mail I sent (incl. aliases)
  3. or_group       -- `{from:a from:b}` behaves as OR
"""
from __future__ import annotations

import sqlite3

from ..config import Config
from .client import GmailClient
from .store import direction_of, my_addresses, parse_addresses


def run(conn: sqlite3.Connection, cfg: Config, client: GmailClient, sample: int = 5) -> dict:
    mine = my_addresses(conn, cfg) | {a for a in client.alias_addresses()}
    mine.add(client.profile()["emailAddress"].lower())
    results: dict[str, dict] = {}

    refs = client.list_message_ids("-in:chats", max_results=sample)
    heads = [client.get_message_headers(r["id"]) for r in refs]
    dates = [h.internal_date for h in heads]
    results["newest_first"] = {"ok": dates == sorted(dates, reverse=True), "dates_ms": dates}

    refs = client.list_message_ids("from:me -in:chats", max_results=sample)
    dirs = [direction_of(client.get_message_headers(r["id"]), mine) for r in refs]
    results["from_me"] = {"ok": bool(dirs) and all(d == "outbound" for d in dirs), "directions": dirs,
                          "note": "" if dirs else "no sent mail found to test with"}

    sender = next((a for h in heads for _, a in parse_addresses(h.headers.get("from")) if a not in mine), None)
    if sender:
        target = next(h.id for h in heads if any(a == sender for _, a in parse_addresses(h.headers.get("from"))))
        hits = client.list_message_ids("{from:" + sender + " from:nobody@invalid.invalid} -in:chats", max_results=50)
        results["or_group"] = {"ok": target in {r["id"] for r in hits}, "sender": sender}
    else:
        results["or_group"] = {"ok": False, "note": "no inbound mail in sample to test with"}

    return {"ok": all(r["ok"] for r in results.values()), "checks": results}
