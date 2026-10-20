"""Pure helpers for Gmail payloads (no API access here)."""
from __future__ import annotations

import base64
import html
import re

from .client import Headers


def is_bulk(h: Headers) -> bool:
    """Mailing lists, newsletters, auto-replies: not person-to-person mail."""
    hd = h.headers
    if hd.get("list-id") or hd.get("list-unsubscribe"):
        return True
    if hd.get("precedence", "").lower() in {"bulk", "list", "junk"}:
        return True
    if hd.get("auto-submitted", "no").lower() not in {"", "no"}:
        return True
    return any(lbl in h.label_ids for lbl in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM"))


def _decode(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def extract_text(payload: dict) -> str:
    """Prefer text/plain; fall back to stripped text/html. Attachments are ignored."""
    plain, htmls = [], []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        if part.get("filename"):
            return
        if mime == "text/plain" and body.get("data"):
            plain.append(_decode(body["data"]))
        elif mime == "text/html" and body.get("data"):
            htmls.append(_decode(body["data"]))
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if htmls:
        return _html_to_text("\n".join(htmls))
    return ""


def strip_quoted(text: str) -> str:
    """Remove quoted reply history ("On ... wrote:" and '>' lines) -- the part I actually wrote."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*On .+ wrote:\s*$", line) or re.match(r"^-{2,}\s*Original Message", line, re.I):
            break
        if re.match(r"^\s*From: .+", line) and lines and not lines[-1].strip():
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()
