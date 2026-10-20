"""Google Contacts and LinkedIn Connections CSV importers.

The raw CSV text is stored as a permanent capture; every contact it touches links back to it.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..captures import add_capture
from ..config import Config
from ..contacts import PersonRecord, ingest_person
from ..db import tx
from ..gmail.store import my_addresses


@dataclass
class CsvReport:
    source: str
    path: str
    rows: int = 0
    skipped: int = 0
    created: int = 0
    unchanged: int = 0               # identical record already imported from an earlier export
    matched_email: int = 0
    matched_linkedin: int = 0
    matched_name_org: int = 0
    merged: int = 0
    merges: list[tuple[int, int]] = field(default_factory=list)
    staged_ids: list[int] = field(default_factory=list)
    capture_id: int | None = None
    already_imported: bool = False


def _clean(v: str | None) -> str:
    return (v or "").strip()


def _split_multi(v: str) -> list[str]:
    return [p.strip() for p in re.split(r"\s*:::\s*", v) if p.strip()]


# ---------------------------------------------------------------------- Google
def parse_google(text: str) -> list[PersonRecord]:
    """Handles both the current export ('First Name', 'E-mail 1 - Value', 'Organization Name')
    and the legacy one ('Name', 'Given Name', 'Organization 1 - Name')."""
    reader = csv.DictReader(io.StringIO(text))
    out = []
    for i, row in enumerate(reader, start=2):              # header is line 1
        first = _clean(row.get("First Name") or row.get("Given Name"))
        middle = _clean(row.get("Middle Name") or row.get("Additional Name"))
        last = _clean(row.get("Last Name") or row.get("Family Name"))
        name = _clean(row.get("Name")) or " ".join(p for p in (first, middle, last) if p)
        emails: list[str] = []
        for k, v in row.items():
            if k and re.match(r"^E-mail \d+ - Value$", k) and v:
                emails += _split_multi(v)
        org = _clean(row.get("Organization Name") or row.get("Organization 1 - Name"))
        title = _clean(row.get("Organization Title") or row.get("Organization 1 - Title"))
        linkedin = None
        for k, v in row.items():
            if k and re.match(r"^Website \d+ - Value$", k) and v and "linkedin.com/in/" in v:
                linkedin = _split_multi(v)[0]
        if not name and not emails:
            continue
        out.append(PersonRecord(display_name=name, emails=emails, org_name=org or None,
                                title=title or None, linkedin_url=linkedin, source="google_csv",
                                source_ref=f"line {i}"))
    return out


# ---------------------------------------------------------------------- LinkedIn
def parse_linkedin(text: str) -> list[PersonRecord]:
    """LinkedIn's Connections.csv starts with a 'Notes:' preamble before the real header."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lstrip('﻿"').startswith("First Name")), None)
    if start is None:
        raise ValueError("no 'First Name' header found; is this LinkedIn's Connections.csv?")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    out = []
    for i, row in enumerate(reader, start=start + 2):
        name = " ".join(p for p in (_clean(row.get("First Name")), _clean(row.get("Last Name"))) if p)
        email = _clean(row.get("Email Address"))
        if not name and not email:
            continue
        out.append(PersonRecord(display_name=name, emails=[email] if email else [],
                                org_name=_clean(row.get("Company")) or None,
                                title=_clean(row.get("Position")) or None,
                                linkedin_url=_clean(row.get("URL")) or None,
                                source="linkedin_csv", source_ref=f"line {i}"))
    return out


PARSERS = {"google": parse_google, "linkedin": parse_linkedin}


def import_csv(conn: sqlite3.Connection, cfg: Config, path: Path | str, source: str) -> CsvReport:
    if source not in PARSERS:
        raise ValueError(f"source must be one of {sorted(PARSERS)}")
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    rep = CsvReport(source=source, path=str(path))
    records = PARSERS[source](text)
    rep.rows = len(records)
    mine = my_addresses(conn, cfg)
    with tx(conn):
        rep.capture_id, created = add_capture(conn, text, f"import:{source}", source_ref=path.name,
                                              status="processed")
        if not created:
            rep.already_imported = True
            return rep
        for rec in records:
            res = ingest_person(conn, rec, capture_id=rep.capture_id, my_emails=mine)
            if res is None:
                rep.skipped += 1
                continue
            setattr(rep, res.action, getattr(rep, res.action) + 1)
            rep.merges += [(res.contact_id, m) for m in res.merged_ids]
            rep.staged_ids += res.staged_ids
    return rep
