"""Configuration: defaults overlaid with app/data/config.toml.

The data directory can be redirected with OUTREACH_DATA_DIR (tests do this).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]          # .../app
PROJECT_ROOT = APP_DIR.parent                            # workspace root

DAY_MS = 86_400_000

# Free-mail domains never count as a shared organisation.
FREEMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "pm.me", "gmx.com", "gmx.net", "fastmail.com", "hey.com", "yandex.com",
    "qq.com", "163.com", "126.com", "mail.com", "zoho.com",
})


@dataclass
class Config:
    data_dir: Path
    # Gmail
    gmail_profile: str = "default"
    body_window_days: float = 1.0
    rolling_sync_enabled: bool = False
    # Identity: every address that counts as "me" (lower-case). Profile address is added at runtime.
    my_emails: list[str] = field(default_factory=list)
    my_orgs: list[str] = field(default_factory=list)
    # Follow-ups
    followup_spacing_days: list[int] = field(default_factory=lambda: [4, 7])
    max_nudges: int = 2
    min_days_before_redraft: int = 3
    dormant_after_days: int = 14
    # Capture review
    auto_approve_high_confidence: bool = False
    # Paths
    captures_dir: Path = PROJECT_ROOT / "captures"
    voice_samples_dir: Path = PROJECT_ROOT / "voice_samples"

    @property
    def body_window_ms(self) -> int:
        return int(self.body_window_days * DAY_MS)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "outreach.db"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    @property
    def client_secret_path(self) -> Path:
        return self.data_dir / "client_secret.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "tokens" / f"{self.gmail_profile}.json"


_KEYS = {
    ("gmail", "profile"): "gmail_profile",
    ("gmail", "body_window_days"): "body_window_days",
    ("gmail", "rolling_sync_enabled"): "rolling_sync_enabled",
    ("me", "emails"): "my_emails",
    ("me", "orgs"): "my_orgs",
    ("followups", "spacing_days"): "followup_spacing_days",
    ("followups", "max_nudges"): "max_nudges",
    ("followups", "min_days_before_redraft"): "min_days_before_redraft",
    ("followups", "dormant_after_days"): "dormant_after_days",
    ("review", "auto_approve_high_confidence"): "auto_approve_high_confidence",
}


def load_config(data_dir: Path | None = None) -> Config:
    data_dir = Path(data_dir or os.environ.get("OUTREACH_DATA_DIR") or APP_DIR / "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(data_dir=data_dir)
    path = data_dir / "config.toml"
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        for (section, key), attr in _KEYS.items():
            if section in raw and key in raw[section]:
                setattr(cfg, attr, raw[section][key])
    cfg.my_emails = [e.strip().lower() for e in cfg.my_emails]
    if cfg.body_window_days <= 0:
        raise ValueError("gmail.body_window_days must be positive")
    return cfg


EXAMPLE_CONFIG = """\
# Outreach config. Edit and save as app/data/config.toml
[gmail]
profile = "default"            # token stored at data/tokens/<profile>.json
body_window_days = 1           # full bodies only for messages newer than this
rolling_sync_enabled = false

[me]
emails = []                    # every address that is you, e.g. ["me@upenn.edu", "me@gmail.com"]
orgs = []                      # your own orgs, e.g. ["University of Pennsylvania"]

[followups]
spacing_days = [4, 7]          # nudge 1 after 4 days, nudge 2 seven days after that
max_nudges = 2
min_days_before_redraft = 3    # never draft to someone whose unanswered email is younger than this
dormant_after_days = 14

[review]
auto_approve_high_confidence = false
"""
