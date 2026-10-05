# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `--tasks` to list all Jira issues assigned to the authenticated user
- Jira client support for creating issues and updating issue summaries and descriptions

Interactive Jira browsing and CLI commands for creating or editing issues remain
planned; see [ROADMAP.md](ROADMAP.md).

## [0.1.0] - 2026-09-28

### Added

- Weekly Tempo worklog planner/filler CLI (`src/cli.py`)
- Jira & Tempo API clients with bounded retry (`src/jira_client.py`)
- Time-slot allocation and gap-filling logic (`src/time_manager.py`)
- Local worklog cache with TTL, `--refresh` to bypass it
- `--dry-run` mode (never POSTs to Tempo)
- TOML app config (`src/config.toml`) + `.env`-based credentials, kept
  separate and git-ignored
- `src/` project layout with root-level `requirements.txt`
- MIT license
