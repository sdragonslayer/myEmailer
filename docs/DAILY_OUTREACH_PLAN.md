# Plan: personalization + daily outreach run (via MCP)

## Context
**Where the repo is now.** Phase 1 is code-complete (55 tests) but has never run against real data: `app/data/` is empty, OAuth isn't set up, nothing is imported. What exists:
- a guarded Gmail client (`gmail/client.py`; the only write it allows is `drafts.create`);
- a SQLite schema that already has `drafts`, `research_notes` (claims need a source URL or an "inferred" basis) and `voice_samples` tables;
- de-dup (`contacts.ingest_person`), the review queue (`staging.py`), warm paths, scores and comms rules.

Phase 2 (MCP server, draft builder, follow-ups) and Phase 4 (voice) are still in TODO.md only.

**What you asked for.** Every day, one run that:
- researches 1 AI-safety person, 1 Penn alum doing interesting work, and 2 startups you could work for;
- finds the right person at each startup: founder or exec at small ones, the closest hiring manager at large ones;
- finds their emails and writes 4 personalized emails, each with a send time between 8 and 9am.

Claude does this through MCP tools.

**Decisions (from your answers):**
- **Drafts only stays.** Each draft gets a random 8:00–9:00 send time, which the daily report lists. You click Gmail's *Schedule send* yourself. No send capability is added and `test_no_send.py` is not touched.
- **Email addresses:** ones the person published themselves, plus the company's pattern (e.g. `first@co.com`) stored as **inferred** with its basis. A personal address is used only if they posted it publicly as a way to reach them. No SMTP probing, no finder APIs, no LinkedIn.
- **Voice:** you hand-pick 15–30 emails into `voice_samples/`. A local redaction pass runs before Claude ever reads them.
- **Resume:** used only to tailor emails. Not attached, not linked.

---

## Part A: Personalization (privacy-first)

The two inputs Claude reads are files you write yourself. Your inbox is never mined for voice.

1. **`me/` folder (gitignored):**
   - `me/resume.md` (or `.pdf`, converted to md once);
   - `me/profile.md`: what roles you want, your story, 3–5 talking points, topics you care about in AI safety, things never to mention;
   - `me/targeting.md`: criteria (startup stage and size, domains, location or remote, companies to exclude);
   - `[me].profile_dir` in `config.py`/`EXAMPLE_CONFIG` points at it.
2. **Voice ingest with local redaction:** new `voice.py` plus the CLI command `outreach voice ingest`.
   - Reads `voice_samples/*.txt|.eml|.md`.
   - Runs `redact()` (pure Python, no network). It replaces phone numbers, street addresses, other email addresses, URLs with tokens, money amounts, ID-like numbers, and any names you list in `me/redact_names.txt`, using placeholders like `[PHONE]` and `[EMAIL]`.
   - Stores **only the redacted text** in `voice_samples`. The `source='folder'` value already exists; `content_hash` de-dups.
   - Prints a redaction diff so you can check it before it's stored. A `--dry-run` flag shows the diff without storing.
   - Originals stay on disk, under your control; Claude is told never to read `voice_samples/` directly (added to CLAUDE.md).
3. **Style stats, computed locally** (`voice.style_stats`): greeting and sign-off frequencies, median word and sentence counts, question rate, and how often you use first names. Split by recipient type, with a small-sample warning (Phase 4 spec).
4. **`.claude/skills/voice/SKILL.md`:** generated from the stats plus 3–5 quoted redacted examples. It's the guide `/daily` and `/draft` follow.
5. **Per-email personalization:** each email is grounded in **one specific, sourced hook** (their paper, talk, launch or blog post) and **one matching point** from `profile.md`/`resume.md`. `check_draft` flags drafts that are too long, have no hook, or reuse the same hook.

## Part B: MCP server (minimal Phase 2 subset needed for the daily run)

New `app/src/outreach/mcp_server.py` (stdio, `mcp` Python SDK added to `pyproject.toml`), the script entry `outreach-mcp`, and a root `.mcp.json`. It reuses existing modules and adds no new Gmail access paths.

| Tool | Backed by |
|---|---|
| `get_profile_context` | reads `me/*.md` + the voice skill stats |
| `search_contacts` / `get_contact` | `contacts.find_by_name/find_by_email/contact_summary` |
| `check_contactable(name, org, emails)` | identity resolution + "already contacted / in pipeline / rejected / follow-up rule" check (see the checks below) |
| `find_warm_paths` | `warm.find_warm_paths` |
| `upsert_contact` | `contacts.ingest_person` (so ambiguity goes to the review queue, never guessed) |
| `save_research` | inserts into `research_notes` (the schema already requires a URL or an inferred basis) |
| `save_contact_email(email, kind, source_url, inferred, basis)` | adds to `contact_emails` with the new `kind`/`confidence` columns |
| `propose_prospect` / `list_prospects` / `set_prospect_status` | new `prospects` table (below) |
| `create_gmail_draft(contact_id, subject, body, planned_send_at, category)` | new `drafting.py` builds the MIME and calls `GmailClient.create_draft`. If Gmail is unavailable, it saves a local draft only (`gmail_draft_id` NULL, marked offline) and doesn't crash. Writes a `drafts` row. |
| `daily_report(date)` | renders the day's drafts, send times, skipped targets and review items |

`check_contactable` blocks drafting when any of these is true:
- the contact's status is not `to_contact`;
- `last_communication.my_last_unanswered` is set and younger than `min_days_before_redraft`;
- a previous prospect for the same person or org was `rejected`;
- the name is ambiguous.

It uses `comms.py` and `header_lookup.lookup_one` (headers only, degrading to `stale`) and doesn't re-derive any of that logic.

**Schema additions** (`schema.sql`, with a `db.py` migration if a DB exists):
- `prospects`:
  - `id`, `run_date`, `category` (`ai_safety|penn_alum|startup`), `contact_id`, `org_id`, `rationale`;
  - `company_size_estimate` (startup rows only), `status` (`proposed|researched|drafted|skipped|rejected`), `skip_reason`;
  - unique on (category, contact_id), so nobody is pitched twice.
- `drafts`: add `planned_send_at`, `category`, `prospect_id`.
- `contact_emails`: add `kind` (`professional|personal`), `inferred` (0/1), `basis`, `source_url`.
- `captures.source` CHECK: add `'research'`, so each day's research bundle (URLs + retrieval timestamps) is a permanent capture that the contacts and notes link back to.

**Permissions** (`.claude/settings.json`):
- read tools, `WebSearch` and `WebFetch` are allowed;
- `upsert_contact`, `save_*` and `create_gmail_draft` are allowed for the headless run (they only touch the local DB and Gmail drafts);
- everything else asks.

## Part C: The daily run

**Slash command `.claude/commands/daily.md`.** Steps Claude follows:
1. `get_profile_context`, then `list_prospects` (so it avoids repeats and respects rejections).
2. **Find candidates** (public web, primary sources first):
   - **AI safety:** recent Alignment Forum / arXiv cs.AI safety papers, lab and research-org blogs (Anthropic, Redwood, METR, Apollo, FAR, UK AISI, MATS mentors).
   - **Penn alum:** Penn Today, Penn Engineering/Wharton news, YC and funding announcements naming a Penn founder. Penn affiliation must be stated in a primary source; otherwise it's labelled inferred and skipped.
   - **Startups:** YC directory, recent funding announcements, company careers pages matched against `targeting.md` and the resume.
3. **For each pick, `check_contactable`.** If the name matches more than one person, **stop on that target**: stage it to the review queue and pick the next candidate. A headless run can't ask you, so it never guesses.
4. **Startup person-picking:**
   - estimate size from the company's own site/careers page or funding news, saved as a sourced claim;
   - **under ~50 people:** CEO, then CTO, then another founder;
   - **larger:** the hiring manager for the team behind a matching open role (from the job post, the team page, or eng blog authors), then the eng/research lead for that area, then a founder;
   - the reasoning is saved as `rationale`.
5. **Emails:**
   - search the person's site, GitHub profile, papers, CV and the company team page;
   - save each address found with its `kind` and source URL;
   - if nothing is published, infer the company pattern from **other addresses published on that domain** and save it as `inferred=1` with that basis;
   - the draft goes to the best professional address; the report lists the alternatives and the inferred flag.
6. **Research notes:** 3–6 sourced claims per target via `save_research`, each with a retrieval date.
7. **Draft** following the voice skill: one hook and one matching point, under ~150 words, with a clear small ask. The startup emails are tailored to the resume, with no attachment.
8. **Send times:** 4 distinct random minutes in 08:00–08:59 local time, at least 5 minutes apart. Stored in `planned_send_at`.
9. `create_gmail_draft` ×4, then `daily_report`, written to `app/data/reports/YYYY-MM-DD.md`. The report lists: each draft → recipient, address (and whether it's inferred), send time, hook + source, a warm path if one exists (suggesting an intro instead), skipped targets and review items.

**Scheduling:**
- A Windows Task Scheduler job at ~06:30 runs `claude -p "/daily" --permission-mode` with the allowed tools from settings. The logs go to `app/data/logs/`.
- A helper script `scripts/register_daily_task.ps1` sets it up. You run it once, after reviewing it.
- If Gmail is revoked or offline, the drafts are saved locally, the report says so, and the run doesn't crash.

**Your 2 minutes each morning:** open the report → open each draft in Gmail → edit if you want → *Schedule send* at the listed time.

## Build order
1. Finish Phase 1 setup with you (OAuth, config, imports). This is needed so `check_contactable` knows who you've already talked to.
2. `me/` folder + `voice.py` (redaction, stats) + `outreach voice ingest` + tests.
3. Schema additions + `drafting.py` (MIME builder, offline fallback) + tests.
4. MCP server + `.mcp.json` + settings permissions + tests for each tool.
5. `/daily` command + voice skill. Do one supervised interactive run together and tune the prompts.
6. Task Scheduler script, then update TODO.md and CLAUDE.md (CLI/MCP tables, the `me/` and `voice_samples` read rules).

## Critical files
- New: `app/src/outreach/{mcp_server.py, drafting.py, voice.py, prospects.py}`, `.mcp.json`, `.claude/settings.json`, `.claude/commands/daily.md`, `.claude/skills/voice/SKILL.md`, `scripts/register_daily_task.ps1`.
- Modified: `schema.sql`, `db.py` (migration), `config.py` (`profile_dir`, daily window settings `send_window = "08:00-09:00"`), `cli.py` (`voice ingest`, `daily-report`), `pyproject.toml` (`mcp`), `.gitignore` (`me/`, `app/data/reports`).
- Reused: `gmail/client.py:create_draft`, `contacts.ingest_person/find_by_*`, `staging.stage`, `comms.py`, `header_lookup.lookup_one`, `warm.find_warm_paths`, `captures.add_capture`.

## Verification
- `cd app && uv run pytest`, all green, including the untouched `test_no_send.py` and `test_body_window.py`. The AST test flags any function whose name contains "send", so helpers get names like `pick_schedule_times`. The `planned_send_at` column string is fine.
- New tests:
  - redaction removes every PII class, and stored samples contain no raw emails or phone numbers;
  - the MIME builder output, and the offline fallback that keeps a local draft;
  - `check_contactable` blocks dormant, already-contacted, rejected and ambiguous targets;
  - the schedule picker returns 4 distinct times in 08:00–08:59 that are at least 5 minutes apart;
  - prospects uniqueness, so nobody is pitched twice;
  - every MCP tool runs against fake Gmail.
- End-to-end:
  - `claude -p "/daily"` interactively on a test profile, then check: 4 drafts in Gmail (the fake in tests, the real Drafts folder live), a report with sources, `outreach audit verify` = ok, and no `messages.send` in `audit.jsonl`;
  - repeat with the token removed and confirm the drafts are saved locally and the run reports `offline`.
