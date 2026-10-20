from __future__ import annotations

import sqlite3

import pytest

from outreach.captures import add_capture
from outreach.cli import main
from outreach.gmail.client import GmailUnavailable, open_client
from outreach.importers.gmail_window import import_window

from conftest import NOW, make_client


def test_open_client_without_token_is_not_configured(cfg):
    with pytest.raises(GmailUnavailable) as e:
        open_client(cfg)
    assert e.value.status == "not_configured"


def test_revoked_token_raises_unavailable_and_is_audited(conn, cfg, audit, mailbox):
    client, svc = make_client(mailbox, audit)
    svc.revoked = True
    with pytest.raises(GmailUnavailable) as e:
        import_window(conn, cfg, client, now=NOW)
    assert e.value.status == "revoked"
    assert audit.entries()[-1]["ok"] is False


def test_cli_works_offline(cfg, capsys, tmp_path):
    # No token on disk: status reports it, local commands still run.
    assert main(["status"]) == 0
    assert '"gmail": "not_configured"' in capsys.readouterr().out
    assert main(["import-window"]) == 2
    p = tmp_path / "c.csv"
    p.write_text("First Name,Last Name,E-mail 1 - Value\nZed,Q,zed@q.com\n", encoding="utf-8")
    assert main(["import-csv", str(p), "--source", "google"]) == 0
    assert main(["warm", "q.com"]) == 0
    assert main(["audit", "verify"]) == 0


def test_capture_raw_text_is_permanent(conn):
    cid, created = add_capture(conn, "met Alice at the conf", "log")
    assert created
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE captures SET raw_text = 'edited' WHERE id = ?", (cid,))
    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        conn.execute("DELETE FROM captures WHERE id = ?", (cid,))
    conn.execute("UPDATE captures SET status = 'processed' WHERE id = ?", (cid,))  # status may change
    assert add_capture(conn, "met Alice at the conf", "log") == (cid, False)       # de-duplicated
