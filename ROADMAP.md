# Roadmap

Tracks what's shipped vs. what the next iterations need to handle. Update
this file whenever a phase starts/completes so intent isn't lost between
sessions.

## v0.1 — Current state (shipped)

- Weekly Tempo worklog planner/filler (`src/cli.py`)
- Config: `src/config.toml` (TOML, schedule/issues) + root `.env` (credentials)
- Local worklog cache with TTL (`.cache/worklogs.json`), `--refresh` to bypass
- `--dry-run` support (never POSTs to Tempo)
- `src/` layout, `requirements.txt` at root

## v0.2 — Read-only Jira ticket browsing (next up)

- `JiraClient.search_issues(jql, fields)` — paginated JQL search
- `JiraClient.get_issue_full(key)` — assignee, components, versions, description
- New `IssueCache` (mirrors `WorklogCache` pattern) — short TTL (2–5 min),
  read-only, never used for writes
- CLI: `--list-issues [jql]` interactive picker (questionary) feeding into
  the existing `plan_task` worklog flow

## v0.3 — Ticket validation

- `--check-issue KEY` — flags missing assignee, missing component/version
- Read-only; no mutation yet

## v0.4 — Ticket mutation: update

- `JiraClient.update_issue(key, **fields)`, `assign_issue(key, account_id)`
- `--edit-issue KEY` — interactive prompt to fix summary/description/assignee/
  component found missing by `--check-issue`
- Always writes directly to the API (no queued/local writes — see
  Architecture notes below)

## v0.5 — Ticket creation

- `JiraClient.create_issue(project, summary, description, issue_type)`
- `--new-issue` guided creation flow
- Highest blast-radius change — ship with `--dry-run` support like the
  existing worklog flow, and confirm-before-submit prompt

## v0.6 — Status transitions

- `JiraClient.get_transitions(key)` / `transition_issue(key, transition_id)`
- `--set-status KEY` — shows valid transitions, applies choice

## Cross-cutting / before wider (external) use

- [ ] Rotate any tokens that were ever exposed during development
- [ ] Add automated tests (currently none) — at least for `time_manager.py`
      slot algebra and `settings.py` config parsing
- [ ] Confirm Jira API token scopes cover write/transition/assign before v0.4
- [ ] Consider packaging (`pyproject.toml` + console-script entry point) once
      the CLI stabilizes, so it's runnable without `cd src`

## Architecture notes (carry forward)

- **No sync daemon / local mirror DB.** Reads use a short-TTL JSON cache
  (same pattern as `WorklogCache`); writes always go straight to the API,
  then update the in-memory cache so the CLI reflects the change without a
  re-fetch. Revisit only if a future need requires full offline browsing.
- Repo intentionally excludes `.env` and `src/config.toml` (real secrets/
  personal schedule) — only `*.example.*` templates are committed.
