"""Command-line entry point: `uv run outreach <command>` from app/."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass

from .audit import AuditLog
from .audit_verify import verify
from .config import EXAMPLE_CONFIG, load_config
from .db import connect
from .gmail.client import GmailUnavailable, open_client, run_auth_flow


def _print(obj) -> None:
    if is_dataclass(obj):
        obj = asdict(obj)
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _summary(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    return {
        "contacts_active": q("SELECT COUNT(*) FROM contacts WHERE merged_into_id IS NULL"),
        "contacts_merged_away": q("SELECT COUNT(*) FROM contacts WHERE merged_into_id IS NOT NULL"),
        "orgs": q("SELECT COUNT(*) FROM orgs"),
        "emails_with_body": q("SELECT COUNT(*) FROM emails WHERE body IS NOT NULL"),
        "emails_headers_only": q("SELECT COUNT(*) FROM emails WHERE body IS NULL"),
        "last_comm_known": q("SELECT COUNT(*) FROM last_communication WHERE found = 1"),
        "last_comm_none_found": q("SELECT COUNT(*) FROM last_communication WHERE found = 0"),
        "my_last_unanswered": q("SELECT COUNT(*) FROM last_communication WHERE my_last_unanswered = 1"),
        "review_queue_pending": q("SELECT COUNT(*) FROM staged_changes WHERE status = 'pending'"),
        "captures": q("SELECT COUNT(*) FROM captures"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="outreach")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-config", help="write an example config.toml if none exists")
    sub.add_parser("auth", help="run the Google OAuth browser flow")
    sub.add_parser("status", help="Gmail connection status + DB summary")
    sub.add_parser("check-gmail", help="verify real Gmail search behaviour the importers rely on (headers only)")
    sub.add_parser("import-window", help="import full mail inside the body window")
    s = sub.add_parser("import-csv", help="import a contacts CSV")
    s.add_argument("path")
    s.add_argument("--source", required=True, choices=["google", "linkedin"])
    s = sub.add_parser("lookup", help="header-only last-communication lookup")
    s.add_argument("--all", action="store_true", help="re-check contacts that already have a result")
    s = sub.add_parser("review", help="review queue")
    s.add_argument("action", choices=["list", "approve", "reject"])
    s.add_argument("id", nargs="?", type=int)
    s.add_argument("--choice", type=int, help="candidate contact id for ambiguous merges")
    s = sub.add_parser("warm", help="warm paths to a person, company or domain")
    s.add_argument("target")
    s = sub.add_parser("scores", help="relationship scores")
    s.add_argument("--limit", type=int, default=30)
    sub.add_parser("report", help="setup report: imports, merges, ambiguities, audit check")
    s = sub.add_parser("audit", help="audit log tools")
    s.add_argument("action", choices=["verify", "tail"])
    args = p.parse_args(argv)

    cfg = load_config()
    if args.cmd == "init-config":
        path = cfg.data_dir / "config.toml"
        if path.exists():
            print(f"{path} already exists")
        else:
            path.write_text(EXAMPLE_CONFIG, encoding="utf-8")
            print(f"wrote {path}; edit [me].emails before importing")
        return 0
    if args.cmd == "auth":
        print(f"authorised as {run_auth_flow(cfg)}")
        return 0

    conn = connect(cfg.db_path)
    audit = AuditLog(cfg.audit_path)

    def gmail():
        return open_client(cfg, audit)

    try:
        if args.cmd == "status":
            try:
                prof = gmail().profile()
                status = {"gmail": "ok", "account": prof["emailAddress"]}
            except GmailUnavailable as e:
                status = {"gmail": e.status, "detail": str(e)}
            _print({**status, **_summary(conn)})
        elif args.cmd == "check-gmail":
            from .gmail.selfcheck import run
            res = run(conn, cfg, gmail())
            _print(res)
            return 0 if res["ok"] else 1
        elif args.cmd == "import-window":
            from .importers.gmail_window import import_window
            _print(import_window(conn, cfg, gmail()))
        elif args.cmd == "import-csv":
            from .importers.csv_contacts import import_csv
            _print(import_csv(conn, cfg, args.path, args.source))
        elif args.cmd == "lookup":
            from .importers.header_lookup import lookup_all
            _print(lookup_all(conn, cfg, gmail(), only_missing=not args.all))
        elif args.cmd == "review":
            from . import staging
            if args.action == "list":
                _print(staging.list_changes(conn))
            elif args.id is None:
                p.error("review approve/reject needs an id")
            elif args.action == "approve":
                print(staging.approve(conn, args.id, choice=args.choice))
            else:
                staging.reject(conn, args.id)
                print("rejected")
        elif args.cmd == "warm":
            from .warm import find_warm_paths
            _print(find_warm_paths(conn, cfg, args.target))
        elif args.cmd == "scores":
            from .contacts import contact_summary
            from .scoring import compute_scores
            scores = sorted(compute_scores(conn, cfg).values(), key=lambda s: -s.score)[: args.limit]
            _print([{**contact_summary(conn, s.contact_id), **s.as_dict()} for s in scores])
        elif args.cmd == "report":
            from . import staging
            from .scoring import compute_scores
            pending = staging.list_changes(conn)
            scores = compute_scores(conn, cfg).values()
            merged = conn.execute(
                "SELECT d.id, d.display_name AS dropped, k.id AS kept_id, k.display_name AS kept "
                "FROM contacts d JOIN contacts k ON k.id = d.merged_into_id").fetchall()
            imports = conn.execute("SELECT id, source, source_ref, captured_at FROM captures "
                                   "WHERE source LIKE 'import:%' ORDER BY id").fetchall()
            _print({
                "summary": _summary(conn),
                "imports": [dict(r) for r in imports],
                "merged_automatically": [dict(r) for r in merged],
                "needs_review": [{"id": c["id"], "kind": c["kind"], "reason": c["reason"],
                                  "field": c["field"], "old": c["old_value"], "new": c["new_value"],
                                  "record": c["payload"].get("record"),
                                  "candidates": c["candidates"]} for c in pending],
                "score_confidence": {lvl: sum(1 for s in scores if s.confidence == lvl)
                                     for lvl in ("low", "medium", "high")},
                "audit": verify(conn, audit),
            })
        elif args.cmd == "audit":
            if args.action == "verify":
                res = verify(conn, audit)
                _print(res)
                return 0 if res["ok"] else 1
            for e in audit.entries()[-50:]:
                print(json.dumps(e, ensure_ascii=False))
    except GmailUnavailable as e:
        print(f"Gmail unavailable ({e.status}): {e}. Local data is untouched; manual tools still work.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
