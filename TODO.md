# TODO

Legend: **[you]** needs you · **[claude]** I can do it · **[together]** needs both.

## Phase 1: finish (blocked on real data)
- [ ] **[you]** Google Cloud: create project, enable the Gmail API, consent screen, and a Desktop
      OAuth client. Save the JSON as `app/data/client_secret.json`. Steps are in
      `docs/PHASE1_SUMMARY.md` → Next steps.
      - Penn account: user type Internal (if the admin allows it).
      - Personal Gmail: External, then "Publish app".
- [ ] **[you]** Run `uv run outreach init-config`, then set `[me].emails` (and `[me].orgs`) in
      `app/data/config.toml`.
- [ ] **[you]** Run `uv run outreach auth`.
- [ ] **[you]** Run `uv run outreach check-gmail` and confirm all 3 checks pass. If any fails, stop
      and tell me before importing.
- [ ] **[you]** Export Google Contacts (Google CSV format) and LinkedIn Connections.csv.
- [ ] **[you]** Run `import-window`, then `import-csv` (google), `import-csv` (linkedin), `lookup`,
      then `report`.
- [ ] **[together]** Go through the `report` output: what was imported, automatic merges, the
      review queue (`outreach review list`), the score confidence spread, and
      `audit verify` = ok.
- [ ] **[together]** Tune if needed: score thresholds, half-life, confidence cutoffs, and whether
      people CC'd on 24h mail should become contacts.
- [ ] **[you]** Decide which Google account this connects to (Penn, personal, or both via profiles).
- [ ] **[you]** Decide whether to commit Phase 1 (nothing is committed yet).

## Phase 2: tracker + MCP tools
- [ ] **[claude]** MCP server (`outreach-mcp`, stdio) plus `.mcp.json`.
- [ ] **[claude]** Tools: `search_contacts`, `get_contact`, `upsert_contact`, `log_interaction`,
      `get_last_communication`, `list_due_follow_ups`, `find_warm_paths`, `save_research`,
      `create_gmail_draft`, `save_draft_record`, `sync_gmail`, plus import tools.
- [ ] **[claude]** Follow-up engine:
      - spacing 4 then 7 days;
      - maximum 2 nudges;
      - no draft if my last email to them is unanswered and under 3 days old;
      - mark dormant after 14 days without a reply.
- [ ] **[claude]** Rolling sync (off by default):
      - fetch new messages via `history.list`, with a fallback to `after:`;
      - detect replies;
      - update draft outcomes.
- [ ] **[claude]** Draft creation: build the MIME message, keep thread replies in the thread, and
      fall back to a local draft when Gmail is offline.
- [ ] **[claude]** Web UI: pipeline board, contact page, due-this-week, sync status / audit log, and
      the review queue.
- [ ] **[claude]** `.claude/settings.json` permissions: read tools allowed, write tools ask.
- [ ] **[claude]** Commands: `/research`, `/draft` (basic), `/followups`, `/warm`, `/sync`.
- [ ] **[claude]** Tests for every MCP tool.
- [ ] **[together]** Check-in.

## Phase 3: manual capture pipeline
- [ ] **[claude]** Capture inputs:
      - quick-capture box in the UI;
      - `captures/` folder ingest, moving files to `_ingested/`;
      - `/log`;
      - structured form.
- [ ] **[claude]** `list_pending_captures`, `get_capture`, `propose_changes` (staging only),
      `mark_capture_processed`.
- [ ] **[claude]** Relative dates resolved against the capture timestamp, done server-side.
- [ ] **[claude]** Review queue: approve, edit or reject; auto-approve for high-confidence changes
      (off by default).
- [ ] **[claude]** Commands: `/log`, `/process`. Optionally, a Task Scheduler job running
      `claude -p "/process"`.
- [ ] **[claude]** Tests:
      - ambiguous names go to the review queue;
      - relative dates resolve against capture time;
      - no approved change lacks a capture link.
- [ ] **[together]** Check-in.

## Phase 4: voice
- [ ] **[you]** Put 20–30 hand-picked emails in `voice_samples/`.
- [ ] **[claude]** Style stats overall and by recipient type (cold/warm/close), with warnings where
      the sample is too small.
- [ ] **[claude]** `.claude/skills/voice/SKILL.md` + `voice.md`, grounded in those numbers and in
      quoted examples.
- [ ] **[claude]** `add_voice_sample`, `find_similar_emails` (full-text search), and `check_draft`
      (flags drafts outside your normal ranges).
- [ ] **[claude]** Upgrade `/draft` to use the voice skill.
- [ ] **[together]** Check-in.

## Phase 5: feedback loop + evaluation
- [ ] **[claude]** Capture the version you actually sent (via sync, or by pasting it in) and store
      the draft→sent diff.
- [ ] **[claude]** Periodic summaries of recurring edits.
- [ ] **[claude]** Dashboard:
      - reply and meeting rates by email type and channel, month over month;
      - edit-size trend.
- [ ] **[claude]** Blind test set (5 real + 5 generated, shuffled) and `/blindtest`.
- [ ] **[together]** Final check against "Done means".

## Done
- [x] Phase 1 code: guarded Gmail client, audit log, 24h import, header-only lookups, CSV imports,
      de-dup, score, warm paths, CLI.
- [x] Phase 1 self-review: 9 correctness fixes, each with a regression test (see
      `docs/PHASE1_SUMMARY.md`).
- [x] `outreach check-gmail`: checks the real mailbox's search behaviour against the fake used in
      tests.
- [x] 55/55 tests passing.
