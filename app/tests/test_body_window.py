"""Proof: messages older than the body window are never fetched with bodies or stored with bodies."""
from __future__ import annotations

import sqlite3

import pytest

from outreach.audit_verify import verify
from outreach.db import set_state
from outreach.gmail.client import OutsideBodyWindow
from outreach.importers.gmail_window import import_window

from conftest import DAY, HOUR, NOW, make_client

OLD_IDS = {"m_25h", "m_3d", "m_30d"}


def _full_ids(svc):
    return {kw["id"] for res, meth, kw in svc.calls
            if res == "messages" and meth == "get" and kw.get("format") == "full"}


@pytest.mark.parametrize("honor_after", [True, False], ids=["query-filters", "query-leaks-old-mail"])
def test_old_messages_never_fetched_with_body(conn, cfg, audit, mailbox, honor_after):
    # honor_after=False simulates Gmail returning old mail despite after: -- the guard must still hold.
    client, svc = make_client(mailbox, audit, me="me@example.com", honor_after=honor_after)
    rep = import_window(conn, cfg, client, now=NOW)

    assert _full_ids(svc).isdisjoint(OLD_IDS)
    assert _full_ids(svc) == {"m_2h", "m_23h", "m_news"}
    assert rep.bodies_stored == 3
    rows = {r["gmail_id"]: r for r in conn.execute("SELECT * FROM emails")}
    for mid in OLD_IDS & rows.keys():
        assert rows[mid]["body"] is None and rows[mid]["fetch_format"] == "metadata"
    for r in conn.execute("SELECT body FROM emails WHERE body IS NOT NULL"):
        assert "SECRET OLD BODY" not in r["body"]
    # Nothing old leaked into any capture either.
    for r in conn.execute("SELECT raw_text FROM captures"):
        assert "SECRET OLD BODY" not in r["raw_text"]
    if not honor_after:
        assert rep.headers_only == 3

    v = verify(conn, audit)
    assert v["ok"], v["problems"]
    assert v["stored_with_body"] == 3


def test_window_is_configurable(conn, cfg, audit, mailbox):
    cfg.body_window_days = 4
    client, svc = make_client(mailbox, audit, honor_after=False)
    import_window(conn, cfg, client, now=NOW)
    assert _full_ids(svc) == {"m_2h", "m_23h", "m_news", "m_25h", "m_3d"}
    assert "m_30d" not in _full_ids(svc)


def test_get_message_full_refuses_old_message_directly(conn, audit, mailbox):
    client, svc = make_client(mailbox, audit)
    with pytest.raises(OutsideBodyWindow) as exc:
        client.get_message_full("m_30d", body_floor_ms=NOW - DAY)
    assert exc.value.headers.id == "m_30d"
    assert "m_30d" not in _full_ids(svc)


def test_database_rejects_old_body_even_if_code_tried(conn):
    set_state(conn, "initial_cutoff_ms", NOW - DAY)
    insert = ("INSERT INTO emails(gmail_id, thread_id, internal_date, direction, fetch_format, body,"
              " body_floor_ms, fetched_at_ms) VALUES (?, 't', ?, 'inbound', 'full', 'x', ?, ?)")
    with pytest.raises(sqlite3.IntegrityError, match="outside permitted window"):
        conn.execute(insert, ("a", NOW - 2 * DAY, NOW - DAY, NOW))           # older than its floor
    with pytest.raises(sqlite3.IntegrityError, match="outside permitted window"):
        conn.execute(insert, ("b", NOW - 3 * DAY, NOW - 5 * DAY, NOW))       # floor before initial cutoff
    with pytest.raises(sqlite3.IntegrityError, match="outside permitted window"):
        conn.execute(insert, ("c", NOW - HOUR, None, NOW))                   # no floor recorded
    conn.execute(insert, ("ok", NOW - HOUR, NOW - DAY, NOW))
    with pytest.raises(sqlite3.IntegrityError, match="outside permitted window"):
        conn.execute("UPDATE emails SET internal_date = ? WHERE gmail_id = 'ok'", (NOW - 9 * DAY,))


def test_rolling_sync_floor_grows_forward_only(conn, cfg):
    from outreach.gmail.store import body_floor_ms
    cfg.rolling_sync_enabled = True
    first = body_floor_ms(conn, cfg, NOW)                       # sets initial cutoff
    assert first == NOW - DAY
    set_state(conn, "last_sync_ms", NOW)
    # Three days later with sync on: bodies since last sync are allowed ("since last sync" setting)...
    assert body_floor_ms(conn, cfg, NOW + 3 * DAY) == NOW
    # ...but never earlier than the initial cutoff, even if last_sync is bogus.
    set_state(conn, "last_sync_ms", NOW - 10 * DAY)
    assert body_floor_ms(conn, cfg, NOW + 3 * DAY) == NOW - DAY
    # Sync off: strict window.
    cfg.rolling_sync_enabled = False
    assert body_floor_ms(conn, cfg, NOW + 3 * DAY) == NOW + 2 * DAY


def test_audit_log_records_every_call(conn, cfg, audit, mailbox):
    client, svc = make_client(mailbox, audit, honor_after=False)
    import_window(conn, cfg, client, now=NOW)
    entries = audit.entries()
    assert len(entries) == len(svc.calls)       # one audit line per executed API request
    lists = [e for e in entries if e["method"] == "messages.list"]
    assert lists and lists[0]["count"] == len(mailbox)
    assert all("q" in e["params"] for e in lists)


def test_verify_detects_tampering(conn, cfg, audit, mailbox):
    client, _ = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)
    # Simulate a body that arrived without going through the client (no audit entry).
    conn.execute("INSERT INTO emails(gmail_id, thread_id, internal_date, direction, fetch_format, body,"
                 " body_floor_ms, fetched_at_ms) VALUES ('rogue', 't', ?, 'inbound', 'full', 'x', ?, ?)",
                 (NOW - HOUR, NOW - DAY, NOW))
    v = verify(conn, audit)
    assert not v["ok"]
    assert any("rogue" in p for p in v["problems"])
