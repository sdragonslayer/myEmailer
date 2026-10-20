"""Warm paths: which of my contacts share a target's company or email domain."""
from __future__ import annotations

import re
import sqlite3

from .config import Config, FREEMAIL_DOMAINS
from .contacts import contact_summary, find_by_email, find_by_name
from .normalize import org_domain, org_key
from .scoring import compute_scores

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _resolve_target(conn: sqlite3.Connection, target: str) -> dict:
    """Work out which orgs/domains the target means. May return person candidates if ambiguous."""
    t = target.strip()
    low = t.lower()
    orgs: set[int] = set()
    domains: set[str] = set()
    exclude: set[int] = set()
    kind = None

    if "@" in low:
        kind = "email"
        d = org_domain(low)
        if d:
            domains.add(d)
        cid = find_by_email(conn, low)
        if cid:
            exclude.add(cid)
            org = conn.execute("SELECT org_id FROM contacts WHERE id = ?", (cid,)).fetchone()[0]
            if org:
                orgs.add(org)
    elif _DOMAIN_RE.match(low):
        kind = "domain"
        if low in FREEMAIL_DOMAINS:
            return {"error": f"{low} is a free-mail domain; it doesn't identify an organisation"}
        domains.add(low)
        orgs |= {r[0] for r in conn.execute("SELECT id FROM orgs WHERE domain = ?", (low,))}
    else:
        key = org_key(t)
        org_rows = conn.execute("SELECT id, domain FROM orgs WHERE name_key = ?", (key,)).fetchall()
        people = find_by_name(conn, t)
        if org_rows:
            kind = "org"
            for r in org_rows:
                orgs.add(r["id"])
                if r["domain"]:
                    domains.add(r["domain"])
        elif people:
            if len(people) > 1:
                return {"ambiguous": True,
                        "candidates": [contact_summary(conn, p) for p in people],
                        "message": f"'{t}' matches {len(people)} contacts; say which one."}
            kind = "person"
            exclude.add(people[0])
            row = conn.execute("SELECT org_id FROM contacts WHERE id = ?", (people[0],)).fetchone()
            if row[0]:
                orgs.add(row[0])
            for (e,) in conn.execute("SELECT email FROM contact_emails WHERE contact_id = ?", (people[0],)):
                d = org_domain(e)
                if d:
                    domains.add(d)
        else:
            kind = "org_name_unknown"
            # Loose org match: target name contained in an org name or vice versa.
            for r in conn.execute("SELECT id, name_key, domain FROM orgs"):
                if key and (key in r["name_key"] or (r["name_key"] and r["name_key"] in key)):
                    orgs.add(r["id"])
                    if r["domain"]:
                        domains.add(r["domain"])
    for (d,) in conn.execute(f"SELECT domain FROM orgs WHERE domain IS NOT NULL AND id IN "
                             f"({','.join('?' * len(orgs))})", list(orgs)) if orgs else []:
        domains.add(d)
    return {"kind": kind, "org_ids": orgs, "domains": domains, "exclude": exclude}


def find_warm_paths(conn: sqlite3.Connection, cfg: Config, target: str, limit: int = 25) -> dict:
    res = _resolve_target(conn, target)
    if "error" in res or res.get("ambiguous"):
        return {"target": target, **res, "paths": []}
    orgs, domains = res["org_ids"], res["domains"]
    if not orgs and not domains:
        return {"target": target, "kind": res["kind"], "paths": [],
                "message": "No org or domain known for this target. Try the company name or domain."}

    hits: dict[int, set[str]] = {}
    if orgs:
        for (cid,) in conn.execute(
                f"SELECT id FROM contacts WHERE merged_into_id IS NULL AND org_id IN ({','.join('?' * len(orgs))})",
                list(orgs)):
            hits.setdefault(cid, set()).add("org")
    for d in domains:
        for (cid,) in conn.execute(
                "SELECT ce.contact_id FROM contact_emails ce JOIN contacts c ON c.id = ce.contact_id "
                "WHERE c.merged_into_id IS NULL AND ce.email LIKE ?", (f"%@{d}",)):
            hits.setdefault(cid, set()).add("domain")
    for cid in res["exclude"]:
        hits.pop(cid, None)

    scores = compute_scores(conn, cfg, contact_ids=list(hits)) if hits else {}
    paths = []
    for cid, shared in hits.items():
        s = scores.get(cid)
        info = contact_summary(conn, cid)
        paths.append({**info, "shared": sorted(shared), "score": s.score if s else 0,
                      "confidence": s.confidence if s else "low",
                      "last_contact": s.last_contact if s else None})
    paths.sort(key=lambda p: (-p["score"], p["name"]))
    return {"target": target, "kind": res["kind"], "domains": sorted(domains), "paths": paths[:limit],
            "total": len(paths)}
