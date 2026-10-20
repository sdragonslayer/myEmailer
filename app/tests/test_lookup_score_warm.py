from __future__ import annotations

from outreach.contacts import find_by_email
from outreach.importers.csv_contacts import import_csv
from outreach.importers.gmail_window import import_window
from outreach.importers.header_lookup import lookup_all, lookup_one
from outreach.scoring import compute_scores
from outreach.warm import find_warm_paths

from conftest import DAY, NOW, make_client

CSV = """First Name,Last Name,Organization Name,E-mail 1 - Value
Bob,Diaz,Globex,bob@globex.com
Carol,Ng,Initech,carol@initech.com
Dan,Wu,Acme,dan@acme.com
Hank,Ito,Acme,hank@acme.com
Ivy,Lo,,ivy@nowhere.org
Pat,Kim,Example Labs,pat@example.com
"""


def _setup(conn, cfg, audit, mailbox, tmp_path):
    client, svc = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)
    p = tmp_path / "g.csv"
    p.write_text(CSV, encoding="utf-8")
    import_csv(conn, cfg, p, "google")
    return client, svc


def test_header_lookup_is_metadata_only(conn, cfg, audit, mailbox, tmp_path):
    client, svc = _setup(conn, cfg, audit, mailbox, tmp_path)
    before = len(svc.calls)
    rep = lookup_all(conn, cfg, client, now=NOW)
    new_calls = svc.calls[before:]
    assert new_calls, "expected Gmail lookups"
    for res, meth, kw in new_calls:
        assert (res, meth) in {("messages", "list"), ("messages", "get"), ("threads", "get")}
        if meth == "get":
            assert kw["format"] == "metadata"
    assert rep.directions_from_local == 2   # Alice, both directions, from the fresh window import
    assert rep.not_found >= 1            # Hank / Ivy have no mail

    carol = find_by_email(conn, "carol@initech.com")
    lc = conn.execute("SELECT * FROM last_communication WHERE contact_id = ?", (carol,)).fetchone()
    assert lc["direction"] == "outbound" and lc["my_last_unanswered"] == 1
    assert lc["subject"] == "following up" and lc["date_ms"] == NOW - 3 * DAY

    bob = find_by_email(conn, "bob@globex.com")
    lc = conn.execute("SELECT * FROM last_communication WHERE contact_id = ?", (bob,)).fetchone()
    assert lc["direction"] == "inbound" and lc["my_last_unanswered"] == 0
    # Header lookups never store a body.
    assert conn.execute("SELECT COUNT(*) FROM emails WHERE gmail_id IN ('m_25h','m_3d','m_30d') "
                        "AND body IS NOT NULL").fetchone()[0] == 0


def test_alice_replied_status_and_two_way_score(conn, cfg, audit, mailbox, tmp_path):
    client, _ = _setup(conn, cfg, audit, mailbox, tmp_path)
    lookup_all(conn, cfg, client, now=NOW)
    alice = find_by_email(conn, "alice@acme.com")
    assert conn.execute("SELECT status FROM contacts WHERE id = ?", (alice,)).fetchone()[0] == "replied"
    s = compute_scores(conn, cfg, now_ms=NOW)[alice]
    assert s.reciprocity == 1.0 and s.recency > 0.99
    assert s.confidence == "low"         # only 2 messages: thin data must be labelled
    assert s.notes


def test_confidence_labels(conn, cfg, audit, mailbox, tmp_path):
    client, _ = _setup(conn, cfg, audit, mailbox, tmp_path)
    lookup_all(conn, cfg, client, now=NOW)
    hank = find_by_email(conn, "hank@acme.com")
    s = compute_scores(conn, cfg, now_ms=NOW)[hank]
    assert s.score == 0 and s.confidence == "low"
    assert "import presence only" in s.notes[0]
    # Add manual interactions -> evidence grows -> confidence rises.
    for i in range(6):
        conn.execute("INSERT INTO interactions(contact_id, channel, occurred_at, created_at) "
                     "VALUES (?, 'in_person', ?, ?)", (hank, "2026-09-01T00:00:00+00:00", "x"))
    s = compute_scores(conn, cfg, now_ms=NOW)[hank]
    assert s.confidence == "high" and s.manual == 1.0


def test_shared_org_with_me(conn, cfg, audit, mailbox, tmp_path):
    _setup(conn, cfg, audit, mailbox, tmp_path)
    pat = find_by_email(conn, "pat@example.com")
    assert compute_scores(conn, cfg, now_ms=NOW)[pat].shared == 1.0


def test_warm_paths_by_company_domain_and_person(conn, cfg, audit, mailbox, tmp_path):
    client, _ = _setup(conn, cfg, audit, mailbox, tmp_path)
    lookup_all(conn, cfg, client, now=NOW)
    by_org = find_warm_paths(conn, cfg, "Acme")
    names = [p["name"] for p in by_org["paths"]]
    assert set(names) == {"Alice Chen", "Dan Wu", "Hank Ito"}
    assert names[0] == "Alice Chen"                      # strongest relationship first
    by_domain = find_warm_paths(conn, cfg, "acme.com")
    assert {p["name"] for p in by_domain["paths"]} == set(names)
    by_person = find_warm_paths(conn, cfg, "Dan Wu")     # target excluded from own paths
    assert {p["name"] for p in by_person["paths"]} == {"Alice Chen", "Hank Ito"}
    assert find_warm_paths(conn, cfg, "gmail.com").get("error")


def test_lookup_without_gmail_uses_stored_data(conn, cfg, audit, mailbox, tmp_path):
    _setup(conn, cfg, audit, mailbox, tmp_path)
    alice = find_by_email(conn, "alice@acme.com")
    res = lookup_one(conn, cfg, None, alice, now=NOW + 5 * DAY)      # stale local data, no Gmail
    assert res["found"] is True and res["stale"] is True and res["two_way"] is True
    bob = find_by_email(conn, "bob@globex.com")
    res = lookup_one(conn, cfg, None, bob, now=NOW + 5 * DAY)
    assert res["found"] is False and res["stale"] is True
