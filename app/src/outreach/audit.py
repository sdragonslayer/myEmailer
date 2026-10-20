"""Append-only JSONL log of every Gmail API call.

Each line: {ts, method, params, format, ids, count, ok, error}. `ids` lists the message/thread IDs
requested (for gets) or returned (for lists), so the log can be reconciled against the database.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .db import now_iso

_lock = threading.Lock()


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, method: str, *, params: dict | None = None, format: str | None = None,
              ids: list[str] | None = None, count: int | None = None,
              ok: bool = True, error: str | None = None) -> None:
        entry = {
            "ts": now_iso(), "method": method, "params": params or {}, "format": format,
            "ids": ids or [], "count": count, "ok": ok, "error": error,
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with _lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
