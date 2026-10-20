"""Regression tests for issues found in the Phase 1 self-review."""
from __future__ import annotations

import pytest

from outreach import staging
from outreach.contacts import find_by_email, merge_contacts
from outreach.importers.csv_contacts import import_csv
from outreach.importers.gmail_window import import_window
from outreach.importers.header_lookup import lookup_all, lookup_one
from outreach.normalize import canonical_linkedin, name_key
from outreach.scoring import compute_scores
from outreach.warm import find_warm_paths

from conftest import DAY, HOUR, NOW, make_client
from fakegmail import FakeMsg


def _csv(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- direction is per contact
def test_cc_on_third_party_mail_is_not_communication(conn, cfg, audit):
    msgs = [FakeMsg("x1", "t", NOW - HOUR, "Bob Diaz <bob@globex.com>", "me@example.com", "fyi",
                    "hello", cc="Carol Ng <carol@initech.com>")]
    client, _ = make_client(msgs, audit)
    import_window(conn, cfg, client, now=NOW)
    carol, bob = find_by_email(conn, "carol@initech.com"), find_by_email(conn, "bob@globex.com")
    assert carol and bob
    scores = compute_scores(conn, cfg, now_ms=NOW)
    assert scores[carol].reciprocity == 0 and scores[carol].evidence == 0
    assert scores[bob].reciprocity == 0.5
    assert conn.execute("SELECT COUNT(*) FROM interactions WHERE contact_id = ?", (carol,)).fetchone()[0] == 0
    # The fresh local data has nothing direct for Carol, so the lookup asks Gmail; Gmail's only hit
    # for her is Bob's mail, which the verified search won't count as "from Carol".
    res = lookup_one(conn, cfg, client, carol, now=NOW)
    assert res["found"] is False
    assert conn.execute("SELECT status FROM contacts WHERE id = ?", (carol,)).fetchone()[0] == "to_contact"


def test_lookup_learns_both_directions(conn, cfg, audit, tmp_path):
    msgs = [
        FakeMsg("a", "t1", NOW - 40 * DAY, "Me <me@example.com>", "Gina <gina@soylent.com>", "hello", ""),
        FakeMsg("b", "t1", NOW - 38 * DAY, "Gina <gina@soylent.com>", "me@example.com", "Re: hello", ""),
        FakeMsg("c", "t2", NOW - 10 * DAY, "Me <me@example.com>", "Gina <gina@soylent.com>", "checking in", ""),
    ]
    client, svc = make_client(msgs, audit)
    import_csv(conn, cfg, _csv(tmp_path, "g.csv", "First Name,Last Name,E-mail 1 - Value\nGina,Ortiz,gina@soylent.com\n"),
               "google")
    gina = find_by_email(conn, "gina@soylent.com")
    res = lookup_one(conn, cfg, client, gina, now=NOW)
    assert res == {"contact_id": gina, "found": True, "direction": "outbound", "two_way": True,
                   "my_last_unanswered": True}
    lc = conn.execute("SELECT * FROM last_communication WHERE contact_id = ?", (gina,)).fetchone()
    assert lc["last_in_ms"] == NOW - 38 * DAY and lc["last_out_ms"] == NOW - 10 * DAY
    assert conn.execute("SELECT status FROM contacts WHERE id = ?", (gina,)).fetchone()[0] == "replied"
    assert compute_scores(conn, cfg, now_ms=NOW)[gina].reciprocity == 1.0
    assert all(kw.get("format") in (None, "metadata") for _, _, kw in svc.calls)


def test_send_as_alias_is_me(conn, cfg, audit):
    msgs = [FakeMsg("s", "t", NOW - HOUR, "Me Alias <me@alias.org>", "Zoe <zoe@zeta.com>", "hi", "body",
                    labels=())]                                 # no SENT label: only the alias identifies me
    client, svc = make_client(msgs, audit)
    svc.aliases = ["me@alias.org"]
    import_window(conn, cfg, client, now=NOW)
    assert find_by_email(conn, "me@alias.org") is None
    assert conn.execute("SELECT direction FROM emails WHERE gmail_id = 's'").fetchone()[0] == "outbound"


# ---------------------------------------------------------------- orgs / dedup
def test_personal_domain_does_not_become_company_domain(conn, cfg, tmp_path):
    csv = ("First Name,Last Name,Organization Name,E-mail 1 - Value\n"
           "Sam,Lee,Stripe,sam@upenn.edu\n"
           "Tess,Ray,University of Pennsylvania,tess@upenn.edu\n"
           "Uma,Fox,Stripe,uma@stripe.com\n")
    import_csv(conn, cfg, _csv(tmp_path, "g.csv", csv), "google")
    stripe = conn.execute("SELECT domain FROM orgs WHERE name = 'Stripe'").fetchone()[0]
    assert stripe == "stripe.com"
    names = {p["name"] for p in find_warm_paths(conn, cfg, "Stripe")["paths"]}
    assert names == {"Sam Lee", "Uma Fox"}                     # Tess (Penn) is not a path to Stripe


LI_V1 = """Notes:
"x"

First Name,Last Name,URL,Email Address,Company,Position,Connected On
John,Smith,https://www.linkedin.com/in/jsmith1,,Globex,Analyst,03 Mar 2024
John,Smith,https://www.linkedin.com/in/jsmith2,,Umbrella,Chemist,04 Mar 2024
Alice,Chen,https://www.linkedin.com/in/alicechen,,Acme,Director,01 Mar 2024
"""

# A later export: different preamble, rows reordered, one new connection, Alice changed title.
LI_V2 = """Notes:
"When exporting your connection data, you may notice..."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Alice,Chen,https://linkedin.com/in/AliceChen/,,Acme,VP Engineering,01 Mar 2024
John,Smith,https://www.linkedin.com/in/jsmith2,,Umbrella,Chemist,04 Mar 2024
John,Smith,https://www.linkedin.com/in/jsmith1,,Globex,Analyst,03 Mar 2024
Kim,Park,https://www.linkedin.com/in/kpark,,Hooli,PM,09 Oct 2026
"""


def test_fresh_linkedin_export_does_not_duplicate(conn, cfg, tmp_path):
    import_csv(conn, cfg, _csv(tmp_path, "v1.csv", LI_V1), "linkedin")
    before = conn.execute("SELECT COUNT(*) FROM contacts WHERE merged_into_id IS NULL").fetchone()[0]
    rep = import_csv(conn, cfg, _csv(tmp_path, "v2.csv", LI_V2), "linkedin")
    after = conn.execute("SELECT COUNT(*) FROM contacts WHERE merged_into_id IS NULL").fetchone()[0]
    assert after == before + 1                                 # only Kim is new
    assert rep.created == 1
    alice = conn.execute("SELECT id, title FROM contacts WHERE name_key = 'alice chen'").fetchall()
    assert len(alice) == 1 and alice[0]["title"] == "Director"   # not overwritten...
    upd = [c for c in staging.list_changes(conn) if c["kind"] == "update_contact"]
    assert [(c["old_value"], c["new_value"]) for c in upd] == [("Director", "VP Engineering")]  # ...staged


def test_rejected_change_is_not_asked_again(conn, cfg, tmp_path):
    import_csv(conn, cfg, _csv(tmp_path, "v1.csv", LI_V1), "linkedin")
    import_csv(conn, cfg, _csv(tmp_path, "v2.csv", LI_V2), "linkedin")
    (upd,) = [c for c in staging.list_changes(conn) if c["kind"] == "update_contact"]
    staging.reject(conn, upd["id"])
    v3 = LI_V2.replace("09 Oct 2026", "10 Oct 2026") + "Lee,Wong,https://www.linkedin.com/in/lwong,,Initech,Eng,11 Oct 2026\n"
    import_csv(conn, cfg, _csv(tmp_path, "v3.csv", v3), "linkedin")
    assert [c for c in staging.list_changes(conn) if c["kind"] == "update_contact"] == []


def test_linkedin_url_links_google_and_linkedin(conn, cfg, tmp_path):
    g = ("First Name,Last Name,Organization Name,E-mail 1 - Value,Website 1 - Value\n"
         "Alice,Chen,,alice@acme.com,http://www.linkedin.com/in/alicechen?trk=abc\n")
    import_csv(conn, cfg, _csv(tmp_path, "g.csv", g), "google")
    # LinkedIn row: different company spelling and no email -- the profile URL is the identifier.
    li = LI_V1.replace(",Acme,", ",Acme Corporation Worldwide,")
    rep = import_csv(conn, cfg, _csv(tmp_path, "li.csv", li), "linkedin")
    assert rep.matched_linkedin == 1
    assert conn.execute("SELECT COUNT(*) FROM contacts WHERE name_key='alice chen' AND merged_into_id IS NULL"
                        ).fetchone()[0] == 1


def test_name_order_and_url_normalisation():
    assert name_key("Chen Alice") == name_key("Alice Chen") == name_key("CHEN, Alice")
    assert name_key("Dr. José A. García") == name_key("jose garcia")
    assert canonical_linkedin("http://linkedin.com/in/AliceChen/?x=1") == "https://www.linkedin.com/in/alicechen"
    assert canonical_linkedin("https://example.com") is None


def test_merge_keeps_furthest_status_and_both_directions(conn, cfg, audit, tmp_path):
    msgs = [FakeMsg("a", "t1", NOW - 20 * DAY, "Me <me@example.com>", "Al <al@a.com>", "hi", ""),
            FakeMsg("b", "t2", NOW - 5 * DAY, "Al <al.personal@gmail.com>", "me@example.com", "yo", "")]
    client, _ = make_client(msgs, audit)
    csv = "First Name,Last Name,E-mail 1 - Value\nAl,Bo,al@a.com\nAlbert,Bo,al.personal@gmail.com\n"
    import_csv(conn, cfg, _csv(tmp_path, "g.csv", csv), "google")
    lookup_all(conn, cfg, client, now=NOW)
    work, personal = find_by_email(conn, "al@a.com"), find_by_email(conn, "al.personal@gmail.com")
    conn.execute("UPDATE contacts SET status = 'meeting' WHERE id = ?", (personal,))
    merge_contacts(conn, work, personal)
    assert conn.execute("SELECT status FROM contacts WHERE id = ?", (work,)).fetchone()[0] == "meeting"
    lc = conn.execute("SELECT * FROM last_communication WHERE contact_id = ?", (work,)).fetchone()
    assert lc["last_out_ms"] == NOW - 20 * DAY and lc["last_in_ms"] == NOW - 5 * DAY
    assert lc["direction"] == "inbound" and lc["my_last_unanswered"] == 0


def test_gmail_selfcheck_passes_on_well_behaved_mailbox(conn, cfg, audit, mailbox):
    from outreach.gmail.selfcheck import run
    client, svc = make_client(mailbox, audit)
    res = run(conn, cfg, client)
    assert res["ok"], res
    assert all(kw.get("format") in (None, "metadata") for _, _, kw in svc.calls)


# ---------------------------------------------------------------- resilience / audit
def test_transient_errors_retry_with_backoff(conn, cfg, audit, mailbox):
    client, svc = make_client(mailbox, audit)
    sleeps = []
    client._sleep = sleeps.append
    svc.fail_next = [429, 503]
    client.profile()
    assert len(sleeps) == 2
    errs = [e for e in audit.entries() if not e["ok"]]
    assert len(errs) == 2 and all("retrying" in e["error"] for e in errs)


def test_non_transient_error_fails_fast(audit, mailbox):
    client, svc = make_client(mailbox, audit)
    client._sleep = lambda s: pytest.fail("should not retry")
    svc.fail_next = [400]
    with pytest.raises(Exception, match="HTTP 400"):
        client.profile()


def test_verify_flags_stored_message_without_logged_fetch(conn, cfg, audit, mailbox):
    client, _ = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)
    conn.execute("INSERT INTO emails(gmail_id, thread_id, internal_date, direction, fetch_format, fetched_at_ms)"
                 " VALUES ('ghost', 't', ?, 'inbound', 'metadata', ?)", (NOW - 9 * DAY, NOW))
    from outreach.audit_verify import verify
    v = verify(conn, audit)
    assert not v["ok"] and any("ghost" in p for p in v["problems"])
