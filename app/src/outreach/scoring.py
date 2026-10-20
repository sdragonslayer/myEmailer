"""Relationship strength from the (deliberately limited) data we hold.

score = 100 * (0.40 recency + 0.30 reciprocity + 0.20 manual + 0.10 shared)
  recency     0.5 ** (days_since_last_contact / 90); 0 if never
  reciprocity 1.0 two-way, 0.5 they wrote only, 0.3 I wrote only, 0 unknown
  manual      weighted non-email interactions (in-person 1.0, call .8, event .6, text .5, other .4), cap 1
  shared      1 if their org is one of mine or they share my (non-free-mail) domain

Confidence reflects how much evidence there is, not how high the score is:
  evidence = email messages observed + manual interactions
  low < 3 <= medium < 6 <= high. CSV presence alone is always low.
"""
from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .comms import direct_stats
from .config import Config
from .gmail.store import my_addresses
from .normalize import org_domain, org_key

HALF_LIFE_DAYS = 90
MANUAL_WEIGHTS = {"in_person": 1.0, "call": 0.8, "event": 0.6, "text": 0.5, "other": 0.4}


@dataclass
class Score:
    contact_id: int
    score: int
    confidence: str
    recipient_type: str          # derived unless overridden on the contact
    recency: float
    reciprocity: float
    manual: float
    shared: float
    last_contact: str | None
    evidence: int
    notes: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


def _parse_iso_ms(s: str) -> int:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def derive_type(score: int) -> str:
    return "cold" if score < 25 else ("warm" if score <= 60 else "close")


def compute_scores(conn: sqlite3.Connection, cfg: Config, *, now_ms: int | None = None,
                   contact_ids: list[int] | None = None) -> dict[int, Score]:
    now_ms = now_ms or int(datetime.now(UTC).timestamp() * 1000)
    mine = my_addresses(conn, cfg)
    my_domains = {d for d in (org_domain(e) for e in mine) if d}
    my_org_keys = {org_key(o) for o in cfg.my_orgs}

    where = "WHERE c.merged_into_id IS NULL"
    params: list = []
    if contact_ids:
        where += f" AND c.id IN ({','.join('?' * len(contact_ids))})"
        params = list(contact_ids)
    contacts = conn.execute(
        f"SELECT c.id, c.recipient_type, o.name_key AS org_key, o.domain AS org_domain "
        f"FROM contacts c LEFT JOIN orgs o ON o.id = c.org_id {where}", params).fetchall()

    # Email evidence: *direct* messages only (cc'd on someone else's mail doesn't count).
    email_stats = direct_stats(conn)
    lastcomm = {r["contact_id"]: r for r in conn.execute("SELECT * FROM last_communication WHERE found = 1")}
    manual = {}
    for r in conn.execute("SELECT contact_id, channel, MAX(occurred_at) last, COUNT(*) n FROM interactions "
                          "WHERE channel != 'email' GROUP BY contact_id, channel"):
        manual.setdefault(r["contact_id"], []).append(r)
    domains = {}
    for r in conn.execute("SELECT contact_id, email FROM contact_emails"):
        d = org_domain(r["email"])
        if d:
            domains.setdefault(r["contact_id"], set()).add(d)

    out = {}
    for c in contacts:
        cid = c["id"]
        notes: list[str] = []
        es = email_stats.get(cid, {})
        n_out, last_out = es.get("outbound", (0, None))
        n_in, last_in = es.get("inbound", (0, None))
        lc = lastcomm.get(cid)
        if lc:
            # Lookup hits are also stored as emails, so max() (not +) avoids double counting.
            if lc["last_out_ms"]:
                n_out, last_out = max(n_out, 1), max(last_out or 0, lc["last_out_ms"])
            if lc["last_in_ms"]:
                n_in, last_in = max(n_in, 1), max(last_in or 0, lc["last_in_ms"])

        last_candidates = [x for x in (last_out, last_in) if x]
        m_rows = manual.get(cid, [])
        m_total = 0.0
        for r in m_rows:
            last_candidates.append(_parse_iso_ms(r["last"]))
            m_total += MANUAL_WEIGHTS.get(r["channel"], 0.4) * r["n"]
        last = max(last_candidates) if last_candidates else None

        recency = 0.5 ** (max(0, now_ms - last) / 86_400_000 / HALF_LIFE_DAYS) if last else 0.0
        if n_in and n_out:
            recip = 1.0
        elif n_in:
            recip = 0.5
        elif n_out:
            recip = 0.3
        else:
            recip = 0.0
        man = min(1.0, m_total / 3)
        shared = 0.0
        if (c["org_key"] and c["org_key"] in my_org_keys) or (domains.get(cid, set()) & my_domains):
            shared = 1.0
        score = round(100 * (0.40 * recency + 0.30 * recip + 0.20 * man + 0.10 * shared))

        n_manual = sum(r["n"] for r in m_rows)
        evidence = n_in + n_out + n_manual
        if evidence < 3:
            conf = "low"
            if evidence == 0:
                notes.append("no communication on record (import presence only)")
            else:
                notes.append(f"only {evidence} data point(s)")
        elif evidence < 6:
            conf = "medium"
        else:
            conf = "high"
        if recip in (0.3, 0.5) and evidence <= 1:
            notes.append("direction known from a single message")
        out[cid] = Score(
            contact_id=cid, score=score, confidence=conf,
            recipient_type=c["recipient_type"] or derive_type(score),
            recency=round(recency, 3), reciprocity=recip, manual=round(man, 3), shared=shared,
            last_contact=datetime.fromtimestamp(last / 1000, UTC).isoformat(timespec="seconds") if last else None,
            evidence=evidence, notes=notes)
    return out
