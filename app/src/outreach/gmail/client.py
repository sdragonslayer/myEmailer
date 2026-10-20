"""The only module that talks to Gmail.

Safety properties enforced here (and tested):
  * Every API call goes through `GmailClient._call`, which only permits an explicit allowlist of
    (resource, method) pairs. There is no send, modify, insert, trash or delete -- not even
    reachable by accident -- and the raw discovery service is never handed out.
  * `messages.get` with format="full" is only issued by `get_message_full`, which first fetches
    metadata, checks internalDate against the caller's body floor, and refuses otherwise.
    format="raw" is never allowed; threads are only fetched as metadata.
  * Every call (success or failure) is written to the audit log.
"""
from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass
from typing import Any

from ..audit import AuditLog
from ..config import Config

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",   # needed for drafts.create; send is blocked in code
]

METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "In-Reply-To",
                    "List-Id", "List-Unsubscribe", "Precedence", "Auto-Submitted"]

# (resource, method). getProfile lives directly on users().
ALLOWED_CALLS = frozenset({
    ("users", "getProfile"),
    ("settings.sendAs", "list"),        # read-only: my aliases, so they're never treated as contacts
    ("messages", "list"),
    ("messages", "get"),
    ("threads", "get"),
    ("history", "list"),
    ("drafts", "create"),
})


class GmailUnavailable(Exception):
    """Gmail cannot be used right now. `status` is 'not_configured' or 'revoked'."""

    def __init__(self, status: str, detail: str = ""):
        super().__init__(f"Gmail unavailable ({status}) {detail}".strip())
        self.status = status


class ForbiddenGmailOperation(Exception):
    pass


class OutsideBodyWindow(Exception):
    """Raised instead of fetching a body. Carries the metadata already fetched (if any)."""

    def __init__(self, msg: str, headers: "Headers | None" = None):
        super().__init__(msg)
        self.headers = headers


_FULL_FETCH_TOKEN = object()   # only get_message_full holds this


@dataclass
class Headers:
    id: str
    thread_id: str
    internal_date: int
    headers: dict[str, str]
    history_id: str | None = None
    label_ids: tuple[str, ...] = ()


class GmailClient:
    def __init__(self, service: Any, audit: AuditLog, *, sleep=time.sleep):
        self.__service = service          # name-mangled; never exposed
        self.audit = audit
        self._sleep = sleep

    # ------------------------------------------------------------------ core gate
    def _call(self, resource: str, method: str, *, _full_token: object = None, **kwargs) -> dict:
        if (resource, method) not in ALLOWED_CALLS:
            self.audit.write(f"{resource}.{method}", params=_loggable(kwargs), ok=False,
                             error="forbidden")
            raise ForbiddenGmailOperation(f"{resource}.{method} is not permitted")
        fmt = kwargs.get("format")
        if resource == "messages" and method == "get":
            if fmt not in ("metadata", "full"):
                raise ForbiddenGmailOperation(f"messages.get format={fmt!r} is not permitted")
            if fmt == "full" and _full_token is not _FULL_FETCH_TOKEN:
                raise ForbiddenGmailOperation("full bodies may only be fetched via get_message_full")
        if resource == "threads" and fmt != "metadata":
            raise ForbiddenGmailOperation("threads may only be fetched with format=metadata")

        target = self.__service.users()
        if resource != "users":
            for part in resource.split("."):
                target = getattr(target, part)()
        for attempt in range(MAX_ATTEMPTS):
            request = getattr(target, method)(userId="me", **kwargs)
            try:
                return request.execute()
            except Exception as e:  # noqa: BLE001 - translated below
                status = _classify_error(e)
                transient = _is_transient(e) and attempt < MAX_ATTEMPTS - 1
                self.audit.write(f"{resource}.{method}", params={**_loggable(kwargs), "attempt": attempt + 1},
                                 format=fmt, ids=_ids_param(kwargs), ok=False,
                                 error=f"{type(e).__name__}: {e}" + (" (retrying)" if transient else ""))
                if status:
                    raise GmailUnavailable(status, str(e)) from e
                if not transient:
                    raise
                self._sleep(min(30.0, 2 ** attempt + random.random()))
        raise AssertionError("unreachable")

    # ------------------------------------------------------------------ reads
    def profile(self) -> dict:
        res = self._call("users", "getProfile")
        self.audit.write("users.getProfile", count=1)
        return res

    def alias_addresses(self) -> list[str]:
        """My own addresses (primary + aliases). Read-only settings call."""
        res = self._call("settings.sendAs", "list")
        addrs = [s["sendAsEmail"].lower() for s in res.get("sendAs", []) if s.get("sendAsEmail")]
        self.audit.write("settings.sendAs.list", ids=[], count=len(addrs))
        return addrs

    def list_message_ids(self, query: str, *, max_results: int | None = None,
                         page_size: int = 500) -> list[dict]:
        """Message refs ({id, threadId}) matching `query`, newest first."""
        out: list[dict] = []
        token = None
        while True:
            size = page_size if max_results is None else min(page_size, max_results - len(out))
            params = {"q": query, "maxResults": size}
            if token:
                params["pageToken"] = token
            res = self._call("messages", "list", **params)
            refs = res.get("messages", [])
            out.extend(refs)
            self.audit.write("messages.list", params={"q": query, "maxResults": size,
                                                      "page": bool(token)},
                             ids=[r["id"] for r in refs], count=len(refs))
            token = res.get("nextPageToken")
            if not token or (max_results is not None and len(out) >= max_results):
                return out

    def get_message_headers(self, msg_id: str) -> Headers:
        res = self._call("messages", "get", id=msg_id, format="metadata",
                         metadataHeaders=METADATA_HEADERS)
        self.audit.write("messages.get", params={"id": msg_id}, format="metadata",
                         ids=[msg_id], count=1)
        return _headers_from(res)

    def get_message_full(self, msg_id: str, *, body_floor_ms: int) -> tuple[Headers, dict]:
        """Fetch a full message only if its internalDate >= body_floor_ms.

        Raises OutsideBodyWindow (after a metadata-only fetch) when the message is too old.
        """
        head = self.get_message_headers(msg_id)
        if head.internal_date < body_floor_ms:
            raise OutsideBodyWindow(f"{msg_id} internalDate {head.internal_date} < floor {body_floor_ms}",
                                    headers=head)
        res = self._call("messages", "get", _full_token=_FULL_FETCH_TOKEN, id=msg_id, format="full")
        self.audit.write("messages.get", params={"id": msg_id, "body_floor_ms": body_floor_ms},
                         format="full", ids=[msg_id], count=1)
        full_head = _headers_from(res)
        if full_head.internal_date < body_floor_ms:   # belt and braces: never trust the first answer alone
            raise OutsideBodyWindow(f"{msg_id} changed internalDate between fetches")
        return full_head, res.get("payload", {})

    def get_thread_headers(self, thread_id: str) -> list[Headers]:
        res = self._call("threads", "get", id=thread_id, format="metadata",
                         metadataHeaders=METADATA_HEADERS)
        msgs = res.get("messages", [])
        self.audit.write("threads.get", params={"id": thread_id}, format="metadata",
                         ids=[m["id"] for m in msgs], count=len(msgs))
        return [_headers_from(m) for m in msgs]

    def list_history(self, start_history_id: str) -> tuple[list[dict], str | None]:
        """Messages added since start_history_id -> (refs, latest historyId)."""
        refs: list[dict] = []
        token = None
        latest = None
        while True:
            params = {"startHistoryId": start_history_id, "historyTypes": ["messageAdded"]}
            if token:
                params["pageToken"] = token
            res = self._call("history", "list", **params)
            page = [m["message"] for h in res.get("history", []) for m in h.get("messagesAdded", [])]
            refs.extend(page)
            latest = res.get("historyId", latest)
            self.audit.write("history.list", params={"startHistoryId": start_history_id},
                             ids=[r["id"] for r in page], count=len(page))
            token = res.get("nextPageToken")
            if not token:
                return refs, latest

    # ------------------------------------------------------------------ the only write
    def create_draft(self, mime_bytes: bytes, thread_id: str | None = None) -> dict:
        message: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime_bytes).decode("ascii")}
        if thread_id:
            message["threadId"] = thread_id
        res = self._call("drafts", "create", body={"message": message})
        self.audit.write("drafts.create", params={"threadId": thread_id},
                         ids=[res.get("id", "")], count=1)
        return res


# ---------------------------------------------------------------------- helpers
def _headers_from(res: dict) -> Headers:
    hdrs = {h["name"].lower(): h["value"] for h in res.get("payload", {}).get("headers", [])}
    return Headers(id=res["id"], thread_id=res["threadId"], internal_date=int(res["internalDate"]),
                   headers=hdrs, history_id=res.get("historyId"),
                   label_ids=tuple(res.get("labelIds", [])))


def _loggable(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if k != "body"}


def _ids_param(kwargs: dict) -> list[str]:
    return [kwargs["id"]] if "id" in kwargs else []


MAX_ATTEMPTS = 5


def _is_transient(e: Exception) -> bool:
    """Rate limits and server errors are retried with backoff; everything else fails fast."""
    status = getattr(getattr(e, "resp", None), "status", None)
    return status in (429, 500, 502, 503, 504) or type(e).__name__ in ("TimeoutError", "ConnectionError")


def _classify_error(e: Exception) -> str | None:
    name = type(e).__name__
    if name == "RefreshError":
        return "revoked"
    status = getattr(getattr(e, "resp", None), "status", None)
    if status in (401,) or "invalid_grant" in str(e):
        return "revoked"
    return None


# ---------------------------------------------------------------------- auth / construction
def open_client(cfg: Config, audit: AuditLog | None = None) -> GmailClient:
    """Build a client from the stored token, or raise GmailUnavailable."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    audit = audit or AuditLog(cfg.audit_path)
    if not cfg.token_path.exists():
        raise GmailUnavailable("not_configured", f"no token at {cfg.token_path}; run `outreach auth`")
    creds = Credentials.from_authorized_user_file(str(cfg.token_path), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise GmailUnavailable("revoked", "token has no refresh token; run `outreach auth`")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            audit.write("oauth.refresh", ok=False, error=str(e))
            raise GmailUnavailable("revoked", str(e)) from e
        cfg.token_path.write_text(creds.to_json(), encoding="utf-8")
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return GmailClient(service, audit)


def run_auth_flow(cfg: Config) -> str:
    """Interactive browser consent. Returns the authorised address."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not cfg.client_secret_path.exists():
        raise FileNotFoundError(f"Put your OAuth Desktop client JSON at {cfg.client_secret_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(cfg.client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.write_text(creds.to_json(), encoding="utf-8")
    return open_client(cfg).profile()["emailAddress"]
