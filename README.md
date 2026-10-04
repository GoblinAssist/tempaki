# 🍣 Tempaki

**Tempo** (worklogs) + **Maki** (sushi roll) — a CLI that rolls up your Jira
tickets and Tempo worklogs into one tidy bite.

Started as a weekly Tempo worklog planner/filler; growing into a full
Jira + Tempo companion (ticket browsing, creation, status/assignee
management, and worklog planning).

## Structure

- **src/** — the CLI
  - `cli.py` — command-line interface / interactive prompts
  - `jira_client.py` — Jira & Tempo API clients
  - `models.py` — domain model (time intervals, worklog entries)
  - `time_manager.py` — slot allocation and local worklog cache
  - `settings.py` — config/credentials loading
  - `config.example.toml` — template config (copy to `config.toml`, git-ignored)
- **requirements.txt** — Python dependencies (root)
- **.env.example** — template credentials file
  (copy to `.env` at the repo root, git-ignored, never commit it)

## Setup

Requires **Python 3.11+** (uses the stdlib `tomllib` parser).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                     # fill in your Jira/Tempo tokens
cp src/config.example.toml src/config.toml  # fill in your issue keys/schedule
```

## Usage

```bash
cd src
python3 cli.py                    # plan the current week
python3 cli.py --week 2026-08-03  # any date inside the target week
python3 cli.py --day 2026-08-12   # read-only: what is logged on that day
python3 cli.py --refresh          # ignore the local cache
python3 cli.py --dry-run          # never POST to Tempo
```

## Roadmap

- [ ] Browse/search Jira tickets and pick one to log time against
- [ ] Validate ticket setup (assignee, component/version present)
- [ ] Create new tickets / edit summary & description
- [ ] Update ticket status

## Security

`.env` and `src/config.toml` hold real API tokens and personal
schedule settings, and are git-ignored — never commit them. Use the
`*.example.*` files as templates.
