from __future__ import annotations

import pytest

from outreach.audit import AuditLog
from outreach.config import load_config
from outreach.db import connect
from outreach.gmail.client import GmailClient

from fakegmail import FakeGmailService, FakeMsg

HOUR = 3_600_000
DAY = 24 * HOUR
NOW = 1_790_000_000_000          # fixed "now" (2026-09-21) for deterministic tests


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTREACH_DATA_DIR", str(tmp_path / "data"))
    c = load_config()
    c.my_emails = ["me@example.com"]
    c.my_orgs = ["Example Labs"]
    c.captures_dir = tmp_path / "captures"
    c.voice_samples_dir = tmp_path / "voice_samples"
    return c


@pytest.fixture
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


@pytest.fixture
def audit(cfg):
    return AuditLog(cfg.audit_path)


def make_client(messages, audit, **kw):
    svc = FakeGmailService(messages, **kw)
    return GmailClient(svc, audit), svc


@pytest.fixture
def mailbox():
    """Messages at 2h, 23h, 25h, 3d and 30d before NOW."""
    return [
        FakeMsg("m_2h", "t1", NOW - 2 * HOUR, "Alice Chen <alice@acme.com>", "me@example.com",
                "Re: intro", "Sounds great, Thursday works.", labels=("INBOX", "CATEGORY_PERSONAL")),
        FakeMsg("m_23h", "t1", NOW - 23 * HOUR, "Me <me@example.com>", "Alice Chen <alice@acme.com>",
                "intro", "Hi Alice, would love to chat about the role.", labels=("SENT",)),
        FakeMsg("m_25h", "t2", NOW - 25 * HOUR, "Bob Diaz <bob@globex.com>", "me@example.com",
                "old thread", "SECRET OLD BODY 25h"),
        FakeMsg("m_3d", "t3", NOW - 3 * DAY, "Me <me@example.com>", "Carol Ng <carol@initech.com>",
                "following up", "SECRET OLD BODY 3d", labels=("SENT",)),
        FakeMsg("m_30d", "t4", NOW - 30 * DAY, "Dan Wu <dan@acme.com>", "me@example.com",
                "ancient", "SECRET OLD BODY 30d"),
        FakeMsg("m_news", "t5", NOW - 1 * HOUR, "Acme News <news@acme.com>", "me@example.com",
                "Weekly digest", "unsubscribe here", extra_headers={"List-Unsubscribe": "<mailto:x>"}),
    ]
