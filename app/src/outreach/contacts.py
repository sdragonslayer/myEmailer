"""Contact and org storage, plus the import-time de-duplication rules.

De-dup rules (deterministic, never guess):
  0. The identical source record was imported before (fingerprint) -> same contact, no changes.
  1. Exact canonical email or LinkedIn profile URL match -> same person.
  2. One record whose identifiers belong to two existing contacts -> those contacts merge
     (the source itself says they are one person).
  3. No email match: same normalised name AND same org -> same person, but only if exactly one
     candidate. More than one candidate, or a name match without org agreement, creates a separate
     contact and stages a merge suggestion for review.
  4. Existing non-empty fields are never overwritten; differing values are staged as old -> new.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from .comms import STATUS_RANK
from .db import now_iso
from .normalize import canonical_email, canonical_linkedin, name_key, org_domain, org_key
from . import staging


@dataclass
class PersonRecord:
    display_name: str
    emails: list[str] = field(default_factory=list)
    org_name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    source: str = "manual"                 # google_csv, linkedin_csv, gmail
    source_ref: str | None = None


@dataclass
class IngestResult:
    contact_id: int
    action: str        # created | unchanged | matched_email | matched_linkedin | matched_name_org | merged
    merged_ids: list[int] = field(default_factory=list)
    staged_ids: list[int] = field(default_factory=list)


# ---------------------------------------------------------------------- orgs
def _domain_label(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "ac", "org", "edu", "gov"}:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else domain


def get_or_create_org(conn: sqlite3.Connection, *, name: str | None = None,
                      domain: str | None = None) -> int | None:
    key = org_key(name)
    if domain:
        row = conn.execute("SELECT id, name, domain FROM orgs WHERE domain = ?", (domain,)).fetchone()
        if row:
            if key and row["name"] == domain:          # upgrade a domain-only name
                conn.execute("UPDATE orgs SET name = ?, name_key = ? WHERE id = ?", (name, key, row["id"]))
            return row["id"]
    if key:
        rows = conn.execute("SELECT id, name, domain FROM orgs WHERE name_key = ?", (key,)).fetchall()
        if len(rows) == 1:
            if domain and not rows[0]["domain"]:
                conn.execute("UPDATE orgs SET domain = ? WHERE id = ?", (domain, rows[0]["id"]))
            if name and rows[0]["name"] == rows[0]["domain"]:
                conn.execute("UPDATE orgs SET name = ? WHERE id = ?", (name, rows[0]["id"]))
            return rows[0]["id"]
        if not rows and not domain:
            # A company name that equals a known domain's label ("Acme" ~ acme.com).
            for r in conn.execute("SELECT id, name, domain FROM orgs WHERE domain IS NOT NULL"):
                if _domain_label(r["domain"]) == key.replace(" ", ""):
                    if r["name"] == r["domain"]:
                        conn.execute("UPDATE orgs SET name = ?, name_key = ? WHERE id = ?",
                                     (name, key, r["id"]))
                    return r["id"]
    if not key and not domain:
        return None
    cur = conn.execute(
        "INSERT INTO orgs(name, name_key, domain, created_at) VALUES (?, ?, ?, ?)",
        (name or domain, key or org_key(_domain_label(domain)), domain, now_iso()),
    )
    return cur.lastrowid


def same_org(conn: sqlite3.Connection, org_id_a: int | None, org_id_b: int | None) -> bool:
    return org_id_a is not None and org_id_a == org_id_b


# ---------------------------------------------------------------------- lookup
def resolve(conn: sqlite3.Connection, contact_id: int) -> int:
    """Follow merged_into_id to the surviving contact."""
    seen = set()
    while True:
        row = conn.execute("SELECT merged_into_id FROM contacts WHERE id = ?", (contact_id,)).fetchone()
        if not row or row[0] is None or contact_id in seen:
            return contact_id
        seen.add(contact_id)
        contact_id = row[0]


def find_by_email(conn: sqlite3.Connection, email: str) -> int | None:
    row = conn.execute("SELECT contact_id FROM contact_emails WHERE email = ?",
                       (canonical_email(email),)).fetchone()
    return resolve(conn, row[0]) if row else None


def find_by_name(conn: sqlite3.Connection, name: str) -> list[int]:
    key = name_key(name)
    if not key:
        return []
    rows = conn.execute(
        "SELECT id FROM contacts WHERE name_key = ? AND merged_into_id IS NULL "
        "UNION SELECT cn.contact_id FROM contact_names cn JOIN contacts c ON c.id = cn.contact_id "
        "WHERE cn.name_key = ? AND c.merged_into_id IS NULL", (key, key)).fetchall()
    return sorted({r[0] for r in rows})


def contact_summary(conn: sqlite3.Connection, contact_id: int) -> dict:
    c = conn.execute("SELECT c.*, o.name AS org_name FROM contacts c LEFT JOIN orgs o ON o.id = c.org_id "
                     "WHERE c.id = ?", (contact_id,)).fetchone()
    emails = [r[0] for r in conn.execute(
        "SELECT email FROM contact_emails WHERE contact_id = ? ORDER BY is_primary DESC, email",
        (contact_id,))]
    return {"id": c["id"], "name": c["display_name"], "org": c["org_name"], "title": c["title"],
            "emails": emails, "linkedin_url": c["linkedin_url"], "status": c["status"]}


# ---------------------------------------------------------------------- writes
def create_contact(conn: sqlite3.Connection, rec: PersonRecord, *, org_id: int | None,
                   capture_id: int | None) -> int:
    ts = now_iso()
    display = rec.display_name or (rec.emails[0] if rec.emails else "(unknown)")
    cur = conn.execute(
        "INSERT INTO contacts(display_name, name_key, org_id, title, linkedin_url, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (display, name_key(rec.display_name), org_id, rec.title or None, rec.linkedin_url or None, ts, ts),
    )
    cid = cur.lastrowid
    for i, e in enumerate(rec.emails):
        conn.execute("INSERT INTO contact_emails(contact_id, email, is_primary, source) VALUES (?, ?, ?, ?)",
                     (cid, canonical_email(e), int(i == 0), rec.source))
    _add_name(conn, cid, rec)
    _add_source(conn, cid, rec, capture_id)
    return cid


def _add_name(conn, cid: int, rec: PersonRecord) -> None:
    if rec.display_name and name_key(rec.display_name):
        conn.execute("INSERT OR IGNORE INTO contact_names(contact_id, name, name_key, source) VALUES (?, ?, ?, ?)",
                     (cid, rec.display_name, name_key(rec.display_name), rec.source))


def _add_source(conn, cid: int, rec: PersonRecord, capture_id: int | None) -> None:
    conn.execute("INSERT INTO contact_sources(contact_id, source, source_ref, source_key, capture_id, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?)",
                 (cid, rec.source, rec.source_ref, record_fingerprint(rec), capture_id, now_iso()))


def merge_contacts(conn: sqlite3.Connection, keep_id: int, drop_id: int) -> None:
    """Fold drop_id into keep_id. Nothing is deleted; drop_id keeps merged_into_id for history."""
    keep_id, drop_id = resolve(conn, keep_id), resolve(conn, drop_id)
    if keep_id == drop_id:
        return
    for table in ("contact_emails", "contact_sources", "interactions", "follow_ups",
                  "research_notes", "drafts"):
        conn.execute(f"UPDATE {table} SET contact_id = ? WHERE contact_id = ?", (keep_id, drop_id))
    conn.execute("UPDATE contact_emails SET is_primary = 0 WHERE contact_id = ? AND is_primary = 1 AND "
                 "rowid != (SELECT MIN(rowid) FROM contact_emails WHERE contact_id = ? AND is_primary = 1)",
                 (keep_id, keep_id))
    conn.execute("UPDATE OR IGNORE contact_names SET contact_id = ? WHERE contact_id = ?", (keep_id, drop_id))
    conn.execute("UPDATE OR IGNORE email_contacts SET contact_id = ? WHERE contact_id = ?", (keep_id, drop_id))
    conn.execute("DELETE FROM email_contacts WHERE contact_id = ?", (drop_id,))      # duplicates of rows already on keep
    conn.execute("DELETE FROM contact_names WHERE contact_id = ?", (drop_id,))
    _merge_last_communication(conn, keep_id, drop_id)
    # Fill keep's empty fields from drop (never overwrite); pipeline status takes the furthest stage.
    k = conn.execute("SELECT * FROM contacts WHERE id = ?", (keep_id,)).fetchone()
    d = conn.execute("SELECT * FROM contacts WHERE id = ?", (drop_id,)).fetchone()
    for f in ("org_id", "title", "linkedin_url", "location", "notes", "recipient_type"):
        if k[f] in (None, "") and d[f] not in (None, ""):
            conn.execute(f"UPDATE contacts SET {f} = ? WHERE id = ?", (d[f], keep_id))
    if STATUS_RANK[d["status"]] > STATUS_RANK[k["status"]]:
        conn.execute("UPDATE contacts SET status = ? WHERE id = ?", (d["status"], keep_id))
    conn.execute("UPDATE contacts SET merged_into_id = ?, updated_at = ? WHERE id = ?",
                 (keep_id, now_iso(), drop_id))
    conn.execute("UPDATE contacts SET updated_at = ? WHERE id = ?", (now_iso(), keep_id))


def _merge_last_communication(conn, keep_id: int, drop_id: int) -> None:
    """Combine per-direction: newest inbound and newest outbound across both contacts."""
    rows = conn.execute("SELECT * FROM last_communication WHERE contact_id IN (?, ?)", (keep_id, drop_id)).fetchall()
    if not rows:
        return
    conn.execute("DELETE FROM last_communication WHERE contact_id IN (?, ?)", (keep_id, drop_id))

    def best(col_ms, col_subj):
        cands = [(r[col_ms], r[col_subj]) for r in rows if r[col_ms]]
        return max(cands) if cands else (None, None)

    li, lo = best("last_in_ms", "last_in_subject"), best("last_out_ms", "last_out_subject")
    newest = max((r for r in rows if r["date_ms"]), key=lambda r: r["date_ms"], default=None)
    conn.execute(
        "INSERT INTO last_communication(contact_id, gmail_id, thread_id, date_ms, direction, subject,"
        " last_in_ms, last_in_subject, last_out_ms, last_out_subject, my_last_unanswered, found,"
        " checked_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (keep_id, newest and newest["gmail_id"], newest and newest["thread_id"], newest and newest["date_ms"],
         newest and newest["direction"], newest and newest["subject"], li[0], li[1], lo[0], lo[1],
         int(bool(lo[0]) and (not li[0] or lo[0] > li[0])), int(newest is not None),
         max(r["checked_at"] for r in rows), (newest or rows[0])["source"]))


def _fill_or_stage(conn, cid: int, rec: PersonRecord, org_id: int | None,
                   capture_id: int | None) -> list[int]:
    """Fill empty fields; stage old -> new for conflicting ones."""
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (cid,)).fetchone()
    staged = []
    incoming = {"org_id": org_id, "title": rec.title or None, "linkedin_url": rec.linkedin_url or None}
    for f, new in incoming.items():
        if new in (None, ""):
            continue
        old = row[f]
        if old in (None, ""):
            conn.execute(f"UPDATE contacts SET {f} = ?, updated_at = ? WHERE id = ?", (new, now_iso(), cid))
        elif str(old) != str(new) and capture_id is not None:
            old_disp, new_disp = old, new
            if f == "org_id":
                old_disp = _org_name(conn, old)
                new_disp = _org_name(conn, new)
            staged.append(staging.stage(
                conn, capture_id=capture_id, kind="update_contact", target_table="contacts",
                target_id=cid, field=f, old_value=old_disp, new_value=new_disp,
                payload={"contact_id": cid, "field": f, "value": new, "source": rec.source,
                         "source_ref": rec.source_ref},
                match_status="unique", confidence="medium",
                reason=f"{rec.source} has a different {f.replace('_id', '')}"))
    return staged


def _org_name(conn, org_id) -> str | None:
    r = conn.execute("SELECT name FROM orgs WHERE id = ?", (org_id,)).fetchone()
    return r[0] if r else None


def record_fingerprint(rec: PersonRecord) -> str:
    """Identity of a source record, stable across re-exports (line numbers are not)."""
    parts = [rec.source, name_key(rec.display_name), ",".join(sorted(rec.emails)),
             org_key(rec.org_name), canonical_linkedin(rec.linkedin_url) or "", rec.title or ""]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def find_by_linkedin(conn: sqlite3.Connection, url: str | None) -> int | None:
    url = canonical_linkedin(url)
    if not url:
        return None
    row = conn.execute("SELECT id FROM contacts WHERE linkedin_url = ? ORDER BY merged_into_id IS NOT NULL, id",
                       (url,)).fetchone()
    return resolve(conn, row[0]) if row else None


def _org_for_record(conn, rec: PersonRecord, emails: list[str]) -> int | None:
    """The record's org. A named company only takes an email domain that plausibly is its own:
    an alum at Stripe writing from upenn.edu must not turn upenn.edu into Stripe's domain."""
    org_dom = next((d for d in (org_domain(e) for e in emails) if d), None)
    if not rec.org_name:
        return get_or_create_org(conn, domain=org_dom) if org_dom else None
    org_id = get_or_create_org(conn, name=rec.org_name)
    if org_dom and _domain_label(org_dom) == org_key(rec.org_name).replace(" ", ""):
        taken = conn.execute("SELECT id FROM orgs WHERE domain = ?", (org_dom,)).fetchone()
        if not taken:
            conn.execute("UPDATE orgs SET domain = ? WHERE id = ? AND domain IS NULL", (org_dom, org_id))
    return org_id


def ingest_person(conn: sqlite3.Connection, rec: PersonRecord, *, capture_id: int | None,
                  my_emails: set[str] = frozenset()) -> IngestResult | None:
    emails = []
    for e in rec.emails:
        ce = canonical_email(e)
        if "@" in ce and ce not in my_emails and ce not in emails:
            emails.append(ce)
    if any(canonical_email(e) in my_emails for e in rec.emails) and not emails:
        return None                                       # it's me
    if not emails and not name_key(rec.display_name):
        return None                                       # nothing to identify anyone by
    rec.emails = emails
    rec.linkedin_url = canonical_linkedin(rec.linkedin_url)

    # 0: the exact same source record was imported before (e.g. a fresh export) -> same contact, nothing new.
    fp = record_fingerprint(rec)
    row = conn.execute("SELECT contact_id FROM contact_sources WHERE source_key = ? LIMIT 1", (fp,)).fetchone()
    if row:
        return IngestResult(resolve(conn, row[0]), "unchanged")

    org_id = _org_for_record(conn, rec, emails)

    # 1 + 2: strong identifiers -- email addresses and LinkedIn profile URL
    matched = []
    for cid in [find_by_email(conn, e) for e in emails] + [find_by_linkedin(conn, rec.linkedin_url)]:
        if cid and cid not in matched:
            matched.append(cid)
    if matched:
        keep = matched[0]
        for other in matched[1:]:
            merge_contacts(conn, keep, other)
        for e in emails:
            if not conn.execute("SELECT 1 FROM contact_emails WHERE email = ?", (e,)).fetchone():
                conn.execute("INSERT INTO contact_emails(contact_id, email, is_primary, source) VALUES (?, ?, 0, ?)",
                             (keep, e, rec.source))
        _add_name(conn, keep, rec)
        _add_source(conn, keep, rec, capture_id)
        staged = _fill_or_stage(conn, keep, rec, org_id, capture_id)
        by_email = any(find_by_email(conn, e) == keep for e in emails)
        action = "merged" if len(matched) > 1 else ("matched_email" if by_email else "matched_linkedin")
        return IngestResult(keep, action, merged_ids=matched[1:], staged_ids=staged)

    # 3: name (+ org)
    candidates = find_by_name(conn, rec.display_name)
    if candidates and org_id is not None:
        same = [c for c in candidates if _org_of(conn, c) == org_id]
        if len(same) == 1 and len(candidates) == 1:
            cid = same[0]
            for e in emails:
                conn.execute("INSERT INTO contact_emails(contact_id, email, is_primary, source) VALUES (?, ?, 0, ?)",
                             (cid, e, rec.source))
            _add_name(conn, cid, rec)
            _add_source(conn, cid, rec, capture_id)
            staged = _fill_or_stage(conn, cid, rec, org_id, capture_id)
            return IngestResult(cid, "matched_name_org", staged_ids=staged)

    cid = create_contact(conn, rec, org_id=org_id, capture_id=capture_id)
    staged = []
    if candidates and capture_id is not None:
        cand_info = [contact_summary(conn, c) for c in candidates]
        if len(candidates) > 1:
            reason = f"name matches {len(candidates)} existing contacts"
        else:
            reason = "same name, no matching email or org"
        staged.append(staging.stage(
            conn, capture_id=capture_id, kind="merge_contacts", target_table="contacts",
            target_id=cid, payload={"new_contact_id": cid, "candidate_ids": candidates,
                                    "record": {"name": rec.display_name, "emails": emails,
                                               "org": rec.org_name, "source": rec.source,
                                               "source_ref": rec.source_ref}},
            match_status="ambiguous", candidates=cand_info, confidence="low", reason=reason))
    return IngestResult(cid, "created", staged_ids=staged)


def _org_of(conn, cid: int) -> int | None:
    r = conn.execute("SELECT org_id FROM contacts WHERE id = ?", (cid,)).fetchone()
    return r[0] if r else None
