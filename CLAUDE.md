# Personal outreach system

Local storage and tools for my outreach. The app makes no LLM calls; you (Claude) are the agent.
- Code lives in `app/` (Python 3.12+, uv). Run commands from `app/`.
- Data lives in `app/data/` (gitignored): `outreach.db`, `audit.jsonl`, `config.toml`, and OAuth
  tokens.
- Progress is tracked in `TODO.md`. Phase write-ups live in `docs/`.

## Standing rules
- **Drafts only.** Nothing in this system sends email, and nothing may be added that can.
  - The only Gmail write is `drafts.create`.
  - Never add SMTP, other mail APIs, or any function, attribute or string named like "send".
    `test_no_send.py` enforces this. If it fails, rename or remove the code; never loosen the test.
- **Minimal inbox access.**
  - Full bodies only inside the body window: the last `body_window_days` (default 1), or since the
    last sync when rolling sync is on. Never earlier than the first import's cutoff.
  - Anything older: headers only.
  - All Gmail access goes through `gmail/client.py`. Never import `googleapiclient` anywhere else.
  - Every call is logged to `app/data/audit.jsonl`.
- **Never guess ambiguous matches.**
  - If a name could be more than one contact, stage it for review and ask me.
  - Never silently overwrite a non-empty field; stage old → new.
  - A rejected proposal is not raised again.
- **Captures are permanent.** Raw capture text is never edited or deleted (DB triggers enforce
  this). Structured data links back to a capture.
- **Only direct mail is communication.**
  - them → me: they sent it.
  - me → them: I sent it with them in To/Cc.
  - Being CC'd on a third party's mail never counts toward direction, reciprocity, "unanswered",
    status or score. Use `comms.py` for this; don't re-derive it.
- **Gmail may be revoked.** Every feature must degrade to stored data (marked `stale`) or manual
  input, never crash.

## Follow-up rules (configurable in `config.toml`)
- Nudge 1 is due 4 days after my unanswered email; nudge 2 is due 7 days after nudge 1.
- Stop after 2 nudges. Mark dormant after 14 more days without a reply.
- Never draft to someone whose unanswered email from me is less than 3 days old.

## Research rules
- Public web only, primary sources first: their own writing, talks, company announcements.
- No LinkedIn scraping.
- Every claim gets a source URL and a retrieval date. Label inferred claims as inferred and state
  the basis.
- If a name matches more than one person, stop and ask.

## Matching / de-dup (`contacts.py`)
Rules apply in this order:
0. An identical source record seen before (fingerprint) → same contact, no change.
1. The same canonical email (gmail dots and `+tags` ignored) or LinkedIn profile URL → same person.
2. One record whose identifiers belong to two contacts → merge.
3. Same name (word order ignored) plus same org, with exactly one candidate → same person.
4. Anything else → a new contact plus a merge suggestion in the review queue.

A company only takes an email domain that matches its name.

## CLI (Phase 1)
| Command | What it does |
|---|---|
| `uv run outreach init-config` | write the `data/config.toml` template (set `[me].emails`) |
| `uv run outreach auth` | Google OAuth browser flow → `data/tokens/<profile>.json` |
| `uv run outreach check-gmail` | check real Gmail search behaviour (ordering, `from:me`, OR-groups); headers only |
| `uv run outreach status` | Gmail status + DB counts |
| `uv run outreach import-window` | full content for mail inside the body window (sent + received) |
| `uv run outreach import-csv PATH --source google\|linkedin` | contacts CSV import + de-dup (re-import safe) |
| `uv run outreach lookup [--all]` | header-only last communication, both directions, for every contact with an email |
| `uv run outreach review list\|approve ID [--choice CID]\|reject ID` | review queue |
| `uv run outreach warm TARGET` | warm paths to a person, company or domain |
| `uv run outreach scores` | relationship scores with breakdown + confidence |
| `uv run outreach report` | setup report: imports, merges, review queue, audit verification |
| `uv run outreach audit verify\|tail` | reconcile the audit log against the DB / show recent calls |

MCP tools and slash commands arrive in Phase 2 (see `TODO.md`).

## Code map (`app/src/outreach/`)
- `gmail/client.py`: the only Gmail access; allowlist, body-window guard, retries, audit.
- `gmail/store.py`, `gmail/parse.py`, `gmail/selfcheck.py`
- `importers/`: `gmail_window.py`, `header_lookup.py`, `csv_contacts.py`
- `contacts.py` (de-dup, merge), `comms.py` (direct-communication rules, status), `scoring.py`,
  `warm.py`
- `staging.py` (review queue), `captures.py`, `audit.py`, `audit_verify.py`, `cli.py`
- `schema.sql`: all tables plus the safety triggers (body window, permanent captures).

## Tests
Run `cd app && uv run pytest`. Everything must pass before you report work as done.
- **Guarantees that must never regress:** `test_body_window.py`, `test_no_send.py`, and the
  capture-immutability tests.
- **Fake Gmail:** tests use `tests/fakegmail.py`. It deliberately exposes dangerous methods so the
  tests prove the client refuses them.
