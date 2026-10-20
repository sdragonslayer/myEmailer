# Phase 1 Summary: Initial Setup from Limited Data

**Status:** Code complete after a self-review pass that fixed 9 correctness issues (see
[Self-review fixes](#self-review-fixes)). 55/55 tests pass against a fake Gmail.
**Not done until** it runs against the real mailbox and the setup report is reviewed. That needs
OAuth credentials and the two CSV exports (see [Next steps](#next-steps)). Nothing has been
committed to git.

---

## What was built

| Area | Module(s) |
|---|---|
| Config (window, follow-up rules, identity) | `app/src/outreach/config.py` |
| SQLite schema (all phases' tables, with safety triggers) | `schema.sql`, `db.py` |
| Gmail client (allowlisted calls only) | `gmail/client.py` |
| Message parsing / storage / body floor | `gmail/parse.py`, `gmail/store.py` |
| Audit log + reconciliation | `audit.py`, `audit_verify.py` |
| 24h full-content import (sent + received) | `importers/gmail_window.py` |
| Header-only last-communication lookup | `importers/header_lookup.py` |
| Google Contacts + LinkedIn CSV import | `importers/csv_contacts.py` |
| De-duplication, orgs, merges | `contacts.py`, `normalize.py` |
| Review queue (staged changes) | `staging.py` |
| Permanent raw captures | `captures.py` |
| Relationship score + confidence | `scoring.py` |
| Warm-path lookup | `warm.py` |
| CLI | `cli.py` |
| Workspace rules | `CLAUDE.md` |

---

## How the guarantees are enforced

### Cannot send email
- `gmail/client.py` is the only module that imports the Google API library. Every call goes
  through one gate that permits only: `getProfile`, `messages.list`, `messages.get`,
  `threads.get` (headers only), `history.list`, and `drafts.create`.
- Any other call (`send`, `modify`, `trash`, `delete`, `insert`, …) is refused before it reaches
  Google, and the refusal is written to the audit log.
- A test scans every source file and fails if send-related code appears: a `.send` attribute, a
  function with "send" in its name, the string `"send"`, or an SMTP/IMAP/POP import. During the
  self-review it caught a harmless function I had named `send_as_addresses`. I renamed the
  function; the test stayed strict.
- The only other call added is a read-only list of Gmail send-as aliases, so they're recognised
  as you.
- Rate-limit (429) and server (5xx) errors are retried up to 5 times with backoff; each attempt is
  audited. Other errors fail fast.

### No old message bodies
- A full message can only be fetched through `get_message_full`, which works in three steps:
  1. Fetch the message's headers and read its date.
  2. Refuse if the message is older than the allowed window (the "body floor").
  3. Fetch the full message, then check the date again.
- The raw format is never allowed, and threads are fetched as headers only.
- The database has its own trigger: it rejects any stored body whose date is before its floor, or
  whose floor is before the first-ever import cutoff.
- Tested with Gmail search **leaking** old mail: old messages still get headers only.

### Audit log
- Every Gmail call becomes one JSON line in `app/data/audit.jsonl`, holding the method, the query,
  the format, the message IDs, the count returned, and success or error.
- `outreach audit verify` checks four things:
  - every stored message, **headers-only rows included**, has a logged fetch;
  - every stored body has a matching full-fetch log entry;
  - no full fetch was made below the floor;
  - no forbidden call was attempted.
- It also lists messages that were fetched but not stored (e.g. from an interrupted run) and flags
  anything that arrived without going through the client.

### Works with Gmail revoked
- A missing token reports `not_configured`; a revoked token reports `revoked`. Commands exit with a
  clear message, and all local commands keep working.
- Header lookups fall back to stored data. Tested.

### Captures are permanent
- Database triggers block editing or deleting a capture's raw text.
- CSV imports and each Gmail import run are stored as captures, the Gmail ones with headers only.
  Every contact and every staged change links back to one.

---

## Matching and de-duplication rules
0. **A record identical to one imported before** (matched by fingerprint, not by line number) maps
   to the same contact with no changes. Re-importing a fresh export therefore creates no
   duplicates.
1. **The same canonical email or LinkedIn profile URL means the same person.** For gmail.com
   addresses, dots and `+tags` are ignored. LinkedIn URLs are normalised (case, `www`, query
   string, trailing slash).
2. **One record whose identifiers belong to two existing contacts:** those contacts merge, because
   the source itself says they are one person.
3. **Same name plus the same company:** treated as the same person, but only if there is exactly
   one candidate. Name word order doesn't matter, so "Chen Alice" matches "Alice Chen".
4. **Anything else** (a name match without company agreement, or several candidates) creates a
   separate contact and puts a merge suggestion in the review queue. Nothing is guessed.
5. **Existing values are never overwritten.** When sources disagree (for example, job titles), the
   difference is queued as old → new. A proposal you rejected is not raised again.
- **Merges:**
  - Nothing is deleted; the merged-away contact keeps a pointer to the one it joined.
  - The kept contact gets the further pipeline stage of the two.
  - Last-contact data is combined per direction.
- **Company domains:** a company only takes an email domain that plausibly belongs to it. Someone
  at "Stripe" writing from `@upenn.edu` doesn't make upenn.edu Stripe's domain.
- **Not contacts:** your own addresses (including Gmail send-as aliases), newsletters, mailing lists
  and no-reply senders.

## Direct communication
Only *direct* messages count toward direction, reciprocity, "unanswered", pipeline status and the
score:
- **them → me:** they sent it.
- **me → them:** I sent it with them in To or Cc.

Being CC'd on a third party's email to me is stored in the contact's history but is not treated as
communication between us.

## Relationship score (0–100)
`score = 100 × (0.40 recency + 0.30 reciprocity + 0.20 manual + 0.10 shared)`

- **Recency:** halves every 90 days since last contact.
- **Reciprocity:**
  - two-way = 1.0
  - they wrote only = 0.5
  - I wrote only = 0.3
- **Manual interactions:** weighted by channel, capped at 1.
  - in-person 1.0
  - call 0.8
  - event 0.6
  - text 0.5
  - other 0.4
- **Shared:** their company is one of mine, or they share my non-free-mail domain.
- **Confidence:** based on the number of data points (emails seen + manual interactions).
  - low: fewer than 3
  - medium: 3–5
  - high: 6 or more
  - Contacts known only from a CSV are always low, and a note explains why. Expect most contacts
    to start out low.
- **Recipient type** is derived from the score unless you set it yourself:
  - cold: below 25
  - warm: 25–60
  - close: above 60

## Last communication (headers only)
- **One search per direction** (newest result only):
  - `{from:addr…}` finds the latest message from them;
  - `from:me {to:addr cc:addr…}` finds the latest from me to them.
- **Each hit is verified against its headers** before it counts, because Gmail search is fuzzier
  than an exact address match.
- **Result:** last contact date and direction, both directions' latest dates and subjects, whether
  the relationship is two-way, and whether my last email is unanswered (my latest is newer than
  theirs).
- **Replies:** when their latest message is found, the lookup fetches that thread's headers to see
  whether it was a reply to an earlier message from me. That's what makes "replied" status
  knowable.
- **Cost:** at most 5 header-only calls per contact. A direction is answered from the database
  instead only when the stored mail is fresh (imported or synced within the last hour).
- **Without Gmail:** you get the stored answer, marked `stale`.

## Warm paths
- The target can be a person's name, a company name, an email or a domain.
- Results are my contacts who share the target's company or email domain, strongest relationship
  first. The target themselves is excluded.
- A name that matches several contacts returns the candidates and asks which one.
- Free-mail domains (gmail.com etc.) are rejected as targets.

---

## Decisions made
- **Account:** not yet decided. The config takes a profile name, and each profile gets its own
  token, so switching accounts is cheap.
- **Rolling sync body rule:** "since last sync". Bodies are allowed back to the last sync, but never
  earlier than the first import's cutoff. With sync off, the strict 24h window applies.
- **Setup lookups:** every contact that has an email address.
- **Capture files:** moved to `captures/_ingested/` after ingest (Phase 3).

## Assumptions to confirm
- Everyone on 24h mail becomes a contact, including CC'd people.
- Follow-up spacing: nudge 2 comes 7 days after nudge 1, not 7 days after the original email.
- The thresholds (90-day half-life, cold/warm/close cutoffs, confidence cutoffs) are starting
  values and can be tuned.

---

## Self-review fixes
Bugs found by reviewing Phase 1 after the first green test run. Each one now has a regression test
in `test_review_fixes.py`.

| # | Problem | Impact | Fix |
|---|---|---|---|
| 1 | Being CC'd on a third party's email counted as "they wrote to me" | Wrong reciprocity, "replied" status and "unanswered"; inflated scores | Only direct messages count (`comms.py`) |
| 2 | The header lookup only found the single latest message, and could match mail where they were merely CC'd | Couldn't tell two-way from one-way; "unanswered" could be wrong | One verified search per direction, plus a thread check to detect real replies |
| 3 | A named company took any email domain on the record (Stripe + `@upenn.edu` → Stripe's domain = upenn.edu) | Warm paths to Stripe would return every Penn contact | A company only takes a domain that matches its name |
| 4 | Re-importing a newer export created duplicates of every ambiguous name | The review queue refills every month | Record fingerprints plus LinkedIn URL as an identifier |
| 5 | Rejected review items came back on the next import | Repeated nagging | Pending or rejected proposals are de-duplicated |
| 6 | Send-as aliases weren't recognised as you | You appear as a contact; wrong direction | Read-only `settings.sendAs.list` |
| 7 | No retry on 429/5xx | A transient error drops a contact from setup | Backoff retry, audited per attempt |
| 8 | The audit check only covered bodies | "Audit log matches what was imported" wasn't actually proven | Every stored row is checked against the log |
| 9 | A merge could lose the more advanced status, and kept only one side's last contact | Wrong pipeline stage after a merge | Furthest stage wins; directions are combined |

Also: name matching ignores word order, and offline lookups return stored data marked `stale`
instead of nothing.

## Known limits (by design or not yet verifiable)
- **The fake Gmail isn't real Gmail.** Run `outreach check-gmail` after `auth` and before
  importing. It checks the three search behaviours the importers rely on against your real mailbox,
  using headers only:
  - results come back newest first;
  - `from:me` returns only mail you sent, aliases included;
  - `{…}` groups behave as OR.
- **The body floor never moves earlier than the first import's cutoff.** Raising
  `body_window_days` later won't fetch older bodies. That follows the "grows forward only" rule;
  it's not a bug.
- **The OAuth token is a plain file** in `app/data/tokens/` (gitignored). It's protected only by
  your Windows account.
- **Most scores start at "low" confidence.** That's honest with headers-only data, not a defect.

## Test coverage (55 tests)
- **Body window** (`test_body_window.py`):
  - old messages are never fetched with bodies, including when Gmail's search leaks old mail;
  - the window is configurable;
  - the database trigger rejects out-of-window bodies;
  - the floor only grows forward;
  - one audit line per API call;
  - tampering is detected.
- **No send** (`test_no_send.py`):
  - the source scan;
  - only the Gmail client module imports the Google API library;
  - draft creation is the only write the allowlist permits;
  - 10 forbidden calls are refused before reaching Gmail;
  - raw and unguarded full fetches are refused;
  - draft creation never sends.
- **Import and de-dup** (`test_import_dedup.py`):
  - both Google CSV formats and the LinkedIn preamble are parsed;
  - the same person is merged across Gmail, Google and LinkedIn;
  - ambiguous names land in the review queue;
  - approving an ambiguous merge requires picking a candidate;
  - re-importing the same CSV changes nothing;
  - every contact and staged change links to a capture.
- **Lookup, score, warm paths** (`test_lookup_score_warm.py`):
  - lookups are headers only;
  - unanswered detection;
  - replied status;
  - confidence labels;
  - shared org with me;
  - warm paths by company, domain and person;
  - lookup works without Gmail.
- **Offline and captures** (`test_offline_and_captures.py`):
  - missing and revoked tokens;
  - the CLI works offline;
  - capture raw text can't be edited or deleted.
- **Self-review regressions** (`test_review_fixes.py`): one or more tests for each of the 9 fixes
  above.

Run them with `cd app && uv run pytest`.

---

## Next steps
1. **OAuth setup.**
   - Create a Google Cloud project, enable the Gmail API, and configure the consent screen:
     - **Penn account:** choose "Internal". If Penn's admin blocks it, you'll find out at the
       `auth` step.
     - **Personal Gmail:** choose "External" and click "Publish app", so you don't have to sign in
       again every 7 days.
   - Scopes: `gmail.readonly` + `gmail.compose`.
   - Create a **Desktop app** OAuth client and save its JSON as `app/data/client_secret.json`.
2. **Configure.** From `app/`, run `uv run outreach init-config`, then list every address that's
   you under `[me].emails` in `app/data/config.toml`.
3. **Run:**
   ```
   uv run outreach auth
   uv run outreach check-gmail
   uv run outreach import-window
   uv run outreach import-csv <path>\contacts.csv --source google
   uv run outreach import-csv <path>\Connections.csv --source linkedin
   uv run outreach lookup
   uv run outreach report
   ```
4. **Review** the `report` output together: what was imported, automatic merges, the review queue,
   and audit verification.
5. **Phase 2:** MCP server and tools, the rest of the tracker logic, rolling sync, the web UI,
   `.mcp.json`, permissions, and the slash commands `/research`, `/draft`, `/followups`, `/warm`
   and `/sync`.
