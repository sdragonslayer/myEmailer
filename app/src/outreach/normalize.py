"""Name / email / org normalisation used for matching and de-duplication."""
from __future__ import annotations

import re
import unicodedata
from email.utils import getaddresses

from .config import FREEMAIL_DOMAINS

_HONORIFICS = {"dr", "mr", "mrs", "ms", "mx", "prof", "sir", "jr", "sr", "ii", "iii", "iv",
               "phd", "md", "mba", "esq"}
_ORG_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "plc",
                 "gmbh", "sa", "ag", "lp", "llp", "the"}


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def canonical_email(addr: str) -> str:
    """Lower-case; for gmail.com strip dots and +tags in the local part (they route identically)."""
    addr = addr.strip().strip("<>").lower()
    if "@" not in addr:
        return addr
    local, domain = addr.rsplit("@", 1)
    if domain == "googlemail.com":
        domain = "gmail.com"
    if domain == "gmail.com":
        local = local.split("+", 1)[0].replace(".", "")
    return f"{local}@{domain}"


def email_domain(addr: str) -> str | None:
    return addr.rsplit("@", 1)[1].lower() if "@" in addr else None


def org_domain(addr: str) -> str | None:
    """Domain that identifies an organisation, or None for free-mail providers."""
    d = email_domain(addr)
    if not d or d in FREEMAIL_DOMAINS:
        return None
    return d


def name_key(name: str | None) -> str:
    """'Dr. José  A. García-López, PhD' -> 'garcia jose lopez'.

    Accents, honorifics and middle initials are dropped; tokens are sorted. Empty if no name.
    """
    if not name:
        return ""
    s = _fold(name)
    if "," in s:                       # "Last, First" -> "First Last" unless it's a suffix
        head, tail = s.split(",", 1)
        tail_tokens = re.findall(r"[a-z0-9]+", tail)
        if tail_tokens and not all(t in _HONORIFICS for t in tail_tokens):
            s = f"{tail} {head}"
        else:
            s = head
    tokens = [t for t in re.findall(r"[a-z0-9]+", s) if t not in _HONORIFICS]
    if len(tokens) > 2:                # drop single-letter middle initials
        tokens = [tokens[0]] + [t for t in tokens[1:-1] if len(t) > 1] + [tokens[-1]]
    # Order-insensitive, so "Chen Alice" (family-name-first) matches "Alice Chen".
    return " ".join(sorted(tokens))


def canonical_linkedin(url: str | None) -> str | None:
    """'http://linkedin.com/in/Alice-Chen/?trk=x' -> 'https://www.linkedin.com/in/alice-chen'."""
    if not url:
        return None
    m = re.search(r"linkedin\.com/(in|pub)/([^/?#\s]+)", url, re.I)
    if not m:
        return None
    return f"https://www.linkedin.com/{m.group(1).lower()}/{m.group(2).lower()}"


def org_key(name: str | None) -> str:
    if not name:
        return ""
    tokens = [t for t in re.findall(r"[a-z0-9]+", _fold(name).replace("&", " and "))
              if t not in _ORG_SUFFIXES]
    return " ".join(tokens)


def parse_addresses(*header_values: str | None) -> list[tuple[str, str]]:
    """[(display_name, canonical_email)] from one or more address headers."""
    vals = [v for v in header_values if v]
    out = []
    for name, addr in getaddresses(vals):
        if "@" in addr:
            out.append((name.strip().strip('"'), canonical_email(addr)))
    return out


def looks_automated(addr: str) -> bool:
    """No-reply / bulk senders are not people."""
    local = addr.split("@", 1)[0]
    return bool(re.search(r"(no-?reply|do-?not-?reply|notifications?|mailer-daemon|bounce|"
                          r"newsletter|digest|alerts?|calendar-notification|postmaster)", local))
