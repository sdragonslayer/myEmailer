"""Proof: the system has no capability to send email."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from outreach.gmail.client import ALLOWED_CALLS, ForbiddenGmailOperation

from conftest import NOW, make_client

SRC = Path(__file__).resolve().parents[1] / "src" / "outreach"


def _py_files():
    return sorted(SRC.rglob("*.py"))


def test_no_send_anywhere_in_source():
    offenders = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.lower() in {"send", "send_message", "sendmail",
                                                                         "send_draft", "send_email"}:
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and "send" in node.name.lower():
                offenders.append(f"{path.name}:{node.lineno} def {node.name}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "send":
                offenders.append(f"{path.name}:{node.lineno} '{node.value}'")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                if any(n and n.split(".")[0] in {"smtplib", "imaplib", "poplib"} for n in names):
                    offenders.append(f"{path.name}:{node.lineno} imports mail transport")
    assert not offenders, offenders


def test_googleapiclient_only_imported_by_gmail_client():
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        if path.name == "client.py" and path.parent.name == "gmail":
            continue
        assert "googleapiclient" not in text, path
        assert "google.oauth2" not in text, path


def test_allowlist_has_no_write_except_draft_create():
    writes = {c for c in ALLOWED_CALLS if c[1] not in {"list", "get", "getProfile"}}
    assert writes == {("drafts", "create")}


@pytest.mark.parametrize("resource,method", [
    ("messages", "send"), ("drafts", "send"), ("messages", "modify"), ("messages", "trash"),
    ("messages", "delete"), ("messages", "insert"), ("messages", "import"), ("drafts", "update"),
    ("drafts", "delete"), ("settings", "updateAutoForwarding"),
])
def test_forbidden_calls_raise_and_never_reach_gmail(audit, resource, method):
    client, svc = make_client([], audit)
    with pytest.raises(ForbiddenGmailOperation):
        client._call(resource, method, id="x")
    assert svc.calls == [] and svc.sent == []
    assert audit.entries()[-1]["error"] == "forbidden"


def test_raw_and_unguarded_full_formats_refused(audit, mailbox):
    client, svc = make_client(mailbox, audit)
    with pytest.raises(ForbiddenGmailOperation):
        client._call("messages", "get", id="m_2h", format="raw")
    with pytest.raises(ForbiddenGmailOperation):
        client._call("messages", "get", id="m_30d", format="full")      # bypassing get_message_full
    with pytest.raises(ForbiddenGmailOperation):
        client._call("threads", "get", id="t1", format="full")
    assert svc.calls == []


def test_service_object_not_exposed(audit):
    client, svc = make_client([], audit)
    public = [getattr(client, n) for n in dir(client) if not n.startswith("_")]
    assert all(v is not svc for v in public)
    assert not hasattr(client, "service") and not hasattr(client, "_service")


def test_draft_create_is_a_draft(audit):
    client, svc = make_client([], audit)
    client.create_draft(b"To: a@b.com\r\nSubject: hi\r\n\r\nbody")
    assert len(svc.drafts) == 1 and svc.sent == []
    assert [(r, m) for r, m, _ in svc.calls] == [("drafts", "create")]
