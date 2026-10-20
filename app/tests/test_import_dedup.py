from __future__ import annotations

from outreach import staging
from outreach.importers.csv_contacts import import_csv, parse_google, parse_linkedin
from outreach.importers.gmail_window import import_window

from conftest import NOW, make_client

GOOGLE_NEW = """First Name,Middle Name,Last Name,Organization Name,Organization Title,E-mail 1 - Label,E-mail 1 - Value,E-mail 2 - Label,E-mail 2 - Value,Website 1 - Value
Alice,,Chen,Acme Inc.,VP Eng,* Work,alice@acme.com,Home,alice.chen@gmail.com,https://www.linkedin.com/in/alicechen
Erin,M.,Park,Hooli,,Work,erin@hooli.com ::: erin.park@hooli.com,,,
John,,Smith,,,,,,,
"""

GOOGLE_LEGACY = """Name,Given Name,Additional Name,Family Name,Organization 1 - Name,Organization 1 - Title,E-mail 1 - Type,E-mail 1 - Value
Frank Li,Frank,,Li,Initech,PM,* Work,frank@initech.com
"""

LINKEDIN = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Alice,Chen,https://www.linkedin.com/in/alicechen,,Acme,Director of Engineering,01 Mar 2024
Erin,Park,https://www.linkedin.com/in/erinpark,,Hooli,Staff Engineer,02 Mar 2024
John,Smith,https://www.linkedin.com/in/jsmith1,,Globex,Analyst,03 Mar 2024
John,Smith,https://www.linkedin.com/in/jsmith2,,Umbrella,Chemist,04 Mar 2024
Gina,Ortiz,https://www.linkedin.com/in/gina,gina@personal.io,Soylent,CEO,05 Mar 2024
"""


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_parse_google_both_formats():
    recs = parse_google(GOOGLE_NEW)
    alice = recs[0]
    assert alice.display_name == "Alice Chen"
    assert alice.emails == ["alice@acme.com", "alice.chen@gmail.com"]
    assert alice.org_name == "Acme Inc." and alice.title == "VP Eng"
    assert alice.linkedin_url.endswith("/alicechen")
    assert recs[1].emails == ["erin@hooli.com", "erin.park@hooli.com"]   # ':::' multi-value
    legacy = parse_google(GOOGLE_LEGACY)
    assert legacy[0].display_name == "Frank Li" and legacy[0].org_name == "Initech"


def test_parse_linkedin_skips_preamble():
    recs = parse_linkedin(LINKEDIN)
    assert [r.display_name for r in recs][:2] == ["Alice Chen", "Erin Park"]
    assert recs[0].emails == [] and recs[4].emails == ["gina@personal.io"]


def test_dedup_across_gmail_google_linkedin(conn, cfg, audit, mailbox, tmp_path):
    client, _ = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)                    # creates Alice from alice@acme.com
    g = import_csv(conn, cfg, _write(tmp_path, "google.csv", GOOGLE_NEW), "google")
    li = import_csv(conn, cfg, _write(tmp_path, "Connections.csv", LINKEDIN), "linkedin")

    alice = [r for r in conn.execute(
        "SELECT * FROM contacts WHERE name_key = 'alice chen' AND merged_into_id IS NULL")]
    assert len(alice) == 1, "Gmail + Google + LinkedIn Alice must be one contact"
    aid = alice[0]["id"]
    emails = {r[0] for r in conn.execute("SELECT email FROM contact_emails WHERE contact_id = ?", (aid,))}
    assert emails == {"alice@acme.com", "alicechen@gmail.com"}   # gmail dots canonicalised
    assert g.matched_email >= 1
    assert li.matched_name_org >= 1                              # 'Acme' ~ 'Acme Inc.' ~ acme.com

    # Never overwrite: Google title 'VP Eng' stays; LinkedIn's differs -> staged old -> new.
    assert alice[0]["title"] == "VP Eng"
    upd = [c for c in staging.list_changes(conn) if c["kind"] == "update_contact" and c["target_id"] == aid]
    assert any(c["field"] == "title" and c["old_value"] == "VP Eng"
               and c["new_value"] == "Director of Engineering" for c in upd)

    # Erin: Google (two hooli emails) + LinkedIn (Hooli, no email) -> one contact.
    erin = conn.execute("SELECT COUNT(*) FROM contacts WHERE name_key='erin park' AND merged_into_id IS NULL").fetchone()[0]
    assert erin == 1


def test_ambiguous_names_land_in_review_queue(conn, cfg, tmp_path):
    import_csv(conn, cfg, _write(tmp_path, "google.csv", GOOGLE_NEW), "google")   # John Smith, no org/email
    import_csv(conn, cfg, _write(tmp_path, "Connections.csv", LINKEDIN), "linkedin")  # two John Smiths

    johns = conn.execute("SELECT id FROM contacts WHERE name_key='john smith' AND merged_into_id IS NULL").fetchall()
    assert len(johns) == 3, "no John Smith may be silently merged"
    merges = [c for c in staging.list_changes(conn) if c["kind"] == "merge_contacts"]
    john_merges = [m for m in merges if m["payload"]["record"]["name"] == "John Smith"]
    assert len(john_merges) == 2
    assert all(m["match_status"] == "ambiguous" for m in john_merges)
    assert all(m["capture_id"] is not None for m in john_merges)
    # The second LinkedIn John matches two existing Johns -> both offered as candidates.
    assert max(len(m["candidates"]) for m in john_merges) == 2


def test_approving_ambiguous_merge_requires_choice(conn, cfg, tmp_path):
    import_csv(conn, cfg, _write(tmp_path, "google.csv", GOOGLE_NEW), "google")
    import_csv(conn, cfg, _write(tmp_path, "Connections.csv", LINKEDIN), "linkedin")
    two = next(c for c in staging.list_changes(conn)
               if c["kind"] == "merge_contacts" and len(c["candidates"]) == 2)
    import pytest
    with pytest.raises(staging.ReviewError, match="pick"):
        staging.approve(conn, two["id"])
    keep = two["payload"]["candidate_ids"][0]
    staging.approve(conn, two["id"], choice=keep)
    new_id = two["payload"]["new_contact_id"]
    assert conn.execute("SELECT merged_into_id FROM contacts WHERE id = ?", (new_id,)).fetchone()[0] == keep


def test_reimporting_same_csv_is_a_noop(conn, cfg, tmp_path):
    p = _write(tmp_path, "google.csv", GOOGLE_NEW)
    import_csv(conn, cfg, p, "google")
    n = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    rep = import_csv(conn, cfg, p, "google")
    assert rep.already_imported
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == n


def test_every_contact_links_to_a_source_capture(conn, cfg, audit, mailbox, tmp_path):
    client, _ = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)
    import_csv(conn, cfg, _write(tmp_path, "google.csv", GOOGLE_NEW), "google")
    import_csv(conn, cfg, _write(tmp_path, "Connections.csv", LINKEDIN), "linkedin")
    orphans = conn.execute("SELECT id FROM contacts WHERE id NOT IN "
                           "(SELECT contact_id FROM contact_sources WHERE capture_id IS NOT NULL) "
                           "AND merged_into_id IS NULL").fetchall()
    assert orphans == []
    assert conn.execute("SELECT COUNT(*) FROM staged_changes WHERE capture_id IS NULL").fetchone()[0] == 0


def test_me_and_bulk_senders_are_not_contacts(conn, cfg, audit, mailbox):
    client, _ = make_client(mailbox, audit)
    import_window(conn, cfg, client, now=NOW)
    assert conn.execute("SELECT 1 FROM contact_emails WHERE email = 'me@example.com'").fetchone() is None
    assert conn.execute("SELECT 1 FROM contact_emails WHERE email = 'news@acme.com'").fetchone() is None
