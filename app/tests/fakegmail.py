"""A fake of the googleapiclient Gmail discovery service. Records every request it executes.

It deliberately exposes *more* than the real client permits (send, modify, trash...), so tests can
prove that GmailClient refuses them rather than relying on the fake lacking them.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

from outreach.normalize import canonical_email, parse_addresses


@dataclass
class FakeMsg:
    id: str
    thread_id: str
    internal_date: int
    from_: str
    to: str
    subject: str
    body: str = ""
    cc: str = ""
    labels: tuple[str, ...] = ("INBOX",)
    extra_headers: dict = field(default_factory=dict)


class _Req:
    def __init__(self, svc, resource, method, kwargs, fn):
        self.svc, self.resource, self.method, self.kwargs, self.fn = svc, resource, method, kwargs, fn

    def execute(self):
        self.svc.calls.append((self.resource, self.method, dict(self.kwargs)))
        if self.svc.revoked:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        if self.svc.fail_next:
            raise HttpError(self.svc.fail_next.pop(0))
        return self.fn(**self.kwargs)


class HttpError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.resp = type("Resp", (), {"status": status})()


class RefreshError(Exception):
    """Same class name as google.auth.exceptions.RefreshError (the client matches on name)."""


class FakeGmailService:
    def __init__(self, messages: list[FakeMsg], me: str = "me@example.com", honor_after: bool = True):
        self.messages = {m.id: m for m in messages}
        self.me = me
        self.honor_after = honor_after
        self.calls: list[tuple[str, str, dict]] = []
        self.revoked = False
        self.fail_next: list[int] = []      # HTTP statuses to raise on the next executes
        self.aliases: list[str] = []
        self.drafts: list[dict] = []
        self.sent: list[dict] = []          # must stay empty forever

    # discovery-style navigation
    def users(self):
        return _Users(self)

    # --------------------------------------------------------------- helpers
    def _matches(self, m: FakeMsg, q: str) -> bool:
        after = re.search(r"after:(\d+)", q)
        if after and self.honor_after and m.internal_date < int(after.group(1)) * 1000:
            return False
        outside = re.sub(r"\{[^}]*\}", " ", q).split()
        if "from:me" in outside and self.me not in m.from_.lower() and \
                not any(a in m.from_.lower() for a in self.aliases):
            return False
        group = re.search(r"\{([^}]*)\}", q)
        if group:
            terms = group.group(1).split()
            ok = False
            for t in terms:
                field_, _, val = t.partition(":")
                hay = {"from": m.from_, "to": m.to, "cc": m.cc}.get(field_, "")
                # Like real Gmail: gmail.com dots/+tags don't matter.
                if canonical_email(val) in {a for _, a in parse_addresses(hay)}:
                    ok = True
            if not ok:
                return False
        return True

    def _headers(self, m: FakeMsg) -> list[dict]:
        h = [{"name": "From", "value": m.from_}, {"name": "To", "value": m.to},
             {"name": "Subject", "value": m.subject}]
        if m.cc:
            h.append({"name": "Cc", "value": m.cc})
        h += [{"name": k, "value": v} for k, v in m.extra_headers.items()]
        return h

    def _resource(self, m: FakeMsg, fmt: str) -> dict:
        payload = {"mimeType": "multipart/alternative", "headers": self._headers(m)}
        if fmt == "full":
            data = base64.urlsafe_b64encode(m.body.encode()).decode().rstrip("=")
            payload["parts"] = [{"mimeType": "text/plain", "body": {"data": data}}]
        return {"id": m.id, "threadId": m.thread_id, "internalDate": str(m.internal_date),
                "labelIds": list(m.labels), "historyId": "100", "payload": payload}


class _Users:
    def __init__(self, svc):
        self.svc = svc

    def getProfile(self, userId):
        return _Req(self.svc, "users", "getProfile", {"userId": userId},
                    lambda userId: {"emailAddress": self.svc.me, "historyId": "100"})

    def messages(self):
        return _Messages(self.svc)

    def settings(self):
        return _Settings(self.svc)

    def threads(self):
        return _Threads(self.svc)

    def history(self):
        return _History(self.svc)

    def drafts(self):
        return _Drafts(self.svc)


class _Messages:
    def __init__(self, svc):
        self.svc = svc

    def list(self, **kw):
        def run(userId, q="", maxResults=100, pageToken=None):
            hits = sorted((m for m in self.svc.messages.values() if self.svc._matches(m, q)),
                          key=lambda m: -m.internal_date)
            start = int(pageToken or 0)
            page = hits[start:start + maxResults]
            res = {"messages": [{"id": m.id, "threadId": m.thread_id} for m in page]}
            if start + maxResults < len(hits):
                res["nextPageToken"] = str(start + maxResults)
            return res
        return _Req(self.svc, "messages", "list", kw, run)

    def get(self, **kw):
        def run(userId, id, format="full", metadataHeaders=None):
            return self.svc._resource(self.svc.messages[id], format)
        return _Req(self.svc, "messages", "get", kw, run)

    # Dangerous methods the real API has. GmailClient must never reach these.
    def send(self, **kw):
        return _Req(self.svc, "messages", "send", kw, lambda **k: self.svc.sent.append(k) or {})

    def modify(self, **kw):
        return _Req(self.svc, "messages", "modify", kw, lambda **k: {})

    def trash(self, **kw):
        return _Req(self.svc, "messages", "trash", kw, lambda **k: {})

    def delete(self, **kw):
        return _Req(self.svc, "messages", "delete", kw, lambda **k: {})

    def insert(self, **kw):
        return _Req(self.svc, "messages", "insert", kw, lambda **k: {})


class _Settings:
    def __init__(self, svc):
        self.svc = svc

    def sendAs(self):
        svc = self.svc

        class _SendAs:
            def list(self, **kw):
                return _Req(svc, "settings.sendAs", "list", kw, lambda userId: {
                    "sendAs": [{"sendAsEmail": a} for a in [svc.me, *svc.aliases]]})

            def create(self, **kw):       # a write the client must never reach
                return _Req(svc, "settings.sendAs", "create", kw, lambda **k: {})
        return _SendAs()


class _Threads:
    def __init__(self, svc):
        self.svc = svc

    def get(self, **kw):
        def run(userId, id, format="full", metadataHeaders=None):
            msgs = [self.svc._resource(m, format) for m in self.svc.messages.values() if m.thread_id == id]
            return {"id": id, "messages": msgs}
        return _Req(self.svc, "threads", "get", kw, run)


class _History:
    def __init__(self, svc):
        self.svc = svc

    def list(self, **kw):
        return _Req(self.svc, "history", "list", kw, lambda **k: {"history": [], "historyId": "100"})


class _Drafts:
    def __init__(self, svc):
        self.svc = svc

    def create(self, **kw):
        def run(userId, body):
            d = {"id": f"d{len(self.svc.drafts) + 1}", "message": body["message"]}
            self.svc.drafts.append(d)
            return d
        return _Req(self.svc, "drafts", "create", kw, run)

    def send(self, **kw):
        return _Req(self.svc, "drafts", "send", kw, lambda **k: self.svc.sent.append(k) or {})
