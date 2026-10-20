-- Outreach schema. Timestamps are ISO-8601 UTC strings unless suffixed _ms (epoch millis).
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS settings  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sync_state(key TEXT PRIMARY KEY, value TEXT);

-- ---------------------------------------------------------------- captures (raw text is permanent)
CREATE TABLE IF NOT EXISTS captures (
    id           INTEGER PRIMARY KEY,
    raw_text     TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('ui','folder','log','form','paste_sent',
                                                 'import:google','import:linkedin','import:gmail')),
    source_ref   TEXT,                       -- file name, CSV path, etc.
    content_hash TEXT NOT NULL UNIQUE,
    captured_at  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processed','failed')),
    processed_at TEXT
);
CREATE TRIGGER IF NOT EXISTS captures_raw_immutable
BEFORE UPDATE OF raw_text, source, source_ref, content_hash, captured_at ON captures
BEGIN SELECT RAISE(ABORT, 'captures raw fields are immutable'); END;
CREATE TRIGGER IF NOT EXISTS captures_no_delete
BEFORE DELETE ON captures
BEGIN SELECT RAISE(ABORT, 'captures are permanent'); END;

-- ---------------------------------------------------------------- people
CREATE TABLE IF NOT EXISTS orgs (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,                -- normalised for matching
    domain     TEXT UNIQUE,
    notes      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orgs_name_key ON orgs(name_key);

CREATE TABLE IF NOT EXISTS contacts (
    id             INTEGER PRIMARY KEY,
    display_name   TEXT NOT NULL,
    name_key       TEXT NOT NULL,            -- normalised first+last
    org_id         INTEGER REFERENCES orgs(id),
    title          TEXT,
    linkedin_url   TEXT,
    location       TEXT,
    status         TEXT NOT NULL DEFAULT 'to_contact'
                   CHECK (status IN ('to_contact','contacted','replied','meeting','dormant')),
    recipient_type TEXT CHECK (recipient_type IN ('cold','warm','close')),  -- manual override
    notes          TEXT,
    merged_into_id INTEGER REFERENCES contacts(id),
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contacts_name_key ON contacts(name_key);

CREATE TABLE IF NOT EXISTS contact_emails (
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    email      TEXT NOT NULL UNIQUE,         -- canonical form
    is_primary INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contact_emails_contact ON contact_emails(contact_id);

CREATE TABLE IF NOT EXISTS contact_names (
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    name       TEXT NOT NULL,
    name_key   TEXT NOT NULL,
    source     TEXT NOT NULL,
    UNIQUE (contact_id, name, source)
);
CREATE INDEX IF NOT EXISTS contact_names_key ON contact_names(name_key);

-- Provenance: which import row / email / capture produced each contact.
CREATE TABLE IF NOT EXISTS contact_sources (
    id         INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    source     TEXT NOT NULL,
    source_ref TEXT,                          -- e.g. CSV row number
    source_key TEXT,                          -- fingerprint of the source record (re-import idempotency)
    capture_id INTEGER REFERENCES captures(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contact_sources_key ON contact_sources(source_key);
CREATE INDEX IF NOT EXISTS contacts_linkedin ON contacts(linkedin_url);

-- ---------------------------------------------------------------- email (bodies only inside the window)
CREATE TABLE IF NOT EXISTS emails (
    id            INTEGER PRIMARY KEY,
    gmail_id      TEXT NOT NULL UNIQUE,
    thread_id     TEXT NOT NULL,
    internal_date INTEGER NOT NULL,          -- epoch ms, from Gmail
    from_addr     TEXT,
    from_name     TEXT,
    to_addrs      TEXT,                      -- JSON list
    cc_addrs      TEXT,                      -- JSON list
    subject       TEXT,
    direction     TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    fetch_format  TEXT NOT NULL CHECK (fetch_format IN ('metadata','full')),
    body          TEXT,
    body_floor_ms INTEGER,                   -- earliest internal_date allowed a body at fetch time
    fetched_at_ms INTEGER NOT NULL,
    CHECK (body IS NULL OR fetch_format = 'full')
);
CREATE INDEX IF NOT EXISTS emails_thread ON emails(thread_id);
CREATE INDEX IF NOT EXISTS emails_date ON emails(internal_date);

-- Defence in depth: the database itself refuses a body outside the permitted window.
CREATE TRIGGER IF NOT EXISTS emails_body_window_insert
BEFORE INSERT ON emails
WHEN NEW.body IS NOT NULL AND (
     NEW.body_floor_ms IS NULL
  OR NEW.internal_date < NEW.body_floor_ms
  OR NEW.body_floor_ms < COALESCE((SELECT CAST(value AS INTEGER) FROM sync_state
                                   WHERE key = 'initial_cutoff_ms'), 9223372036854775807))
BEGIN SELECT RAISE(ABORT, 'email body outside permitted window'); END;

CREATE TRIGGER IF NOT EXISTS emails_body_window_update
BEFORE UPDATE OF body, body_floor_ms, internal_date ON emails
WHEN NEW.body IS NOT NULL AND (
     NEW.body_floor_ms IS NULL
  OR NEW.internal_date < NEW.body_floor_ms
  OR NEW.body_floor_ms < COALESCE((SELECT CAST(value AS INTEGER) FROM sync_state
                                   WHERE key = 'initial_cutoff_ms'), 9223372036854775807))
BEGIN SELECT RAISE(ABORT, 'email body outside permitted window'); END;

CREATE TABLE IF NOT EXISTS email_contacts (
    email_id   INTEGER NOT NULL REFERENCES emails(id),
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    role       TEXT NOT NULL CHECK (role IN ('from','to','cc')),
    PRIMARY KEY (email_id, contact_id, role)
);
CREATE INDEX IF NOT EXISTS email_contacts_contact ON email_contacts(contact_id);

-- Header-only "last communication" per contact (from setup lookup or on-demand).
-- Only *direct* messages count: inbound = sent by them; outbound = sent by me with them in To/Cc.
-- Mail where they were merely cc'd by a third party is not communication between us.
CREATE TABLE IF NOT EXISTS last_communication (
    contact_id        INTEGER PRIMARY KEY REFERENCES contacts(id),
    gmail_id          TEXT,                  -- most recent direct message, either direction
    thread_id         TEXT,
    date_ms           INTEGER,
    direction         TEXT CHECK (direction IN ('outbound','inbound')),
    subject           TEXT,
    last_in_ms        INTEGER,               -- most recent message from them
    last_in_subject   TEXT,
    last_out_ms       INTEGER,               -- most recent message from me to them
    last_out_subject  TEXT,
    my_last_unanswered INTEGER NOT NULL DEFAULT 0,
    found             INTEGER NOT NULL,       -- 0 = searched, nothing found
    checked_at        TEXT NOT NULL,
    source            TEXT NOT NULL CHECK (source IN ('window_import','header_lookup','sync'))
);

-- ---------------------------------------------------------------- tracking
CREATE TABLE IF NOT EXISTS interactions (
    id               INTEGER PRIMARY KEY,
    contact_id       INTEGER NOT NULL REFERENCES contacts(id),
    channel          TEXT NOT NULL CHECK (channel IN ('email','in_person','call','text','event','other')),
    direction        TEXT CHECK (direction IN ('outbound','inbound','mutual')),
    occurred_at      TEXT NOT NULL,
    summary          TEXT,
    email_id         INTEGER REFERENCES emails(id),
    capture_id       INTEGER REFERENCES captures(id),
    staged_change_id INTEGER REFERENCES staged_changes(id),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS interactions_contact ON interactions(contact_id);

CREATE TABLE IF NOT EXISTS follow_ups (
    id               INTEGER PRIMARY KEY,
    contact_id       INTEGER NOT NULL REFERENCES contacts(id),
    due_date         TEXT NOT NULL,           -- YYYY-MM-DD
    reason           TEXT,
    nudge_number     INTEGER,                 -- NULL for manual follow-ups
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','skipped','blocked')),
    anchor_email_id  INTEGER REFERENCES emails(id),
    capture_id       INTEGER REFERENCES captures(id),
    staged_change_id INTEGER REFERENCES staged_changes(id),
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_notes (
    id           INTEGER PRIMARY KEY,
    contact_id   INTEGER REFERENCES contacts(id),
    org_id       INTEGER REFERENCES orgs(id),
    claim        TEXT NOT NULL,
    source_url   TEXT,
    retrieved_at TEXT NOT NULL,
    inferred     INTEGER NOT NULL DEFAULT 0,
    basis        TEXT,
    capture_id   INTEGER REFERENCES captures(id),
    staged_change_id INTEGER REFERENCES staged_changes(id),
    created_at   TEXT NOT NULL,
    CHECK (contact_id IS NOT NULL OR org_id IS NOT NULL),
    CHECK (inferred = 1 OR source_url IS NOT NULL OR capture_id IS NOT NULL),
    CHECK (inferred = 0 OR basis IS NOT NULL)
);

-- ---------------------------------------------------------------- review queue
CREATE TABLE IF NOT EXISTS staged_changes (
    id              INTEGER PRIMARY KEY,
    capture_id      INTEGER NOT NULL REFERENCES captures(id),
    kind            TEXT NOT NULL,           -- create_contact, update_contact, merge_contacts, add_interaction, ...
    payload_json    TEXT NOT NULL,
    target_table    TEXT,
    target_id       INTEGER,
    field           TEXT,
    old_value       TEXT,
    new_value       TEXT,
    match_status    TEXT NOT NULL CHECK (match_status IN ('unique','new','ambiguous')),
    candidates_json TEXT,
    confidence      TEXT NOT NULL CHECK (confidence IN ('high','medium','low')),
    reason          TEXT,
    date_phrase     TEXT,
    resolved_date   TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','edited','rejected','auto_approved')),
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    applied_ref     TEXT                      -- e.g. "contacts:12"
);
CREATE INDEX IF NOT EXISTS staged_status ON staged_changes(status);

-- ---------------------------------------------------------------- drafting + voice
CREATE TABLE IF NOT EXISTS drafts (
    id              INTEGER PRIMARY KEY,
    contact_id      INTEGER REFERENCES contacts(id),
    gmail_draft_id  TEXT,
    thread_id       TEXT,
    purpose         TEXT,
    email_type      TEXT CHECK (email_type IN ('cold','warm','close','followup')),
    subject         TEXT,
    draft_text      TEXT NOT NULL,
    final_sent_text TEXT,
    sent_email_id   INTEGER REFERENCES emails(id),
    diff_text       TEXT,
    edit_ratio      REAL,
    voice_flags_json TEXT,
    outcome         TEXT NOT NULL DEFAULT 'pending'
                    CHECK (outcome IN ('pending','no_reply','replied','meeting','declined')),
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

CREATE TABLE IF NOT EXISTS voice_samples (
    id             INTEGER PRIMARY KEY,
    text           TEXT NOT NULL,
    content_hash   TEXT NOT NULL UNIQUE,
    source         TEXT NOT NULL CHECK (source IN ('folder','sent_window','draft_edit','manual')),
    recipient_type TEXT CHECK (recipient_type IN ('cold','warm','close')),
    file_path      TEXT,
    email_id       INTEGER REFERENCES emails(id),
    added_at       TEXT NOT NULL
);
