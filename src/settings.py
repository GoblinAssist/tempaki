"""Loading and validation of config.toml (single source of truth for schedules).

Credentials are kept out of config.toml entirely and are read from
environment variables (optionally loaded from a local .env file), so secrets
never live in a file that could realistically be checked into version
control alongside the schedule settings.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from models import Interval, MeetingBlock, WEEKDAY_KEYS, hours_to_minutes, parse_hhmm

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.toml")
REQUIRED_ENV_KEYS = ("JIRA_URL", "JIRA_USER", "JIRA_TOKEN", "TEMPO_TOKEN")


class ConfigError(RuntimeError):
    """Raised when config.toml or the environment credentials are unusable."""


@dataclass(frozen=True)
class Credentials:
    jira_url: str
    jira_user: str
    jira_token: str
    tempo_token: str

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "Credentials":
        """Read JIRA_URL/JIRA_USER/JIRA_TOKEN/TEMPO_TOKEN from the environment.

        If `env_path` points at an existing file, it is loaded first (without
        overriding variables already exported in the shell).
        """
        if env_path is not None and env_path.exists():
            load_dotenv(env_path, override=False)

        missing = [key for key in REQUIRED_ENV_KEYS if not os.environ.get(key)]
        if missing:
            source = f" (checked {env_path})" if env_path else ""
            raise ConfigError(f"Missing environment variable(s){source}: {', '.join(missing)}")

        return cls(
            jira_url=os.environ["JIRA_URL"].rstrip("/"),
            jira_user=os.environ["JIRA_USER"],
            jira_token=os.environ["JIRA_TOKEN"],
            tempo_token=os.environ["TEMPO_TOKEN"],
        )


@dataclass(frozen=True)
class AppConfig:
    env_path: Path
    work_window: Interval
    target_minutes: int
    min_slot_minutes: int
    cache_path: Path
    cache_ttl_minutes: int
    meetings_issue: str
    support_issue: str
    default_project: str
    meeting_blocks: tuple[MeetingBlock, ...]
    free_days: frozenset[date]
    sprint_board_id: int | None
    default_component: str | None

    def blocks_for(self, day: date) -> tuple[MeetingBlock, ...]:
        return tuple(sorted(
            (block for block in self.meeting_blocks if block.applies_to(day)),
            key=lambda block: block.interval,
        ))

    def is_free_day(self, day: date) -> bool:
        """True for configured holidays/days off - no target hours or meetings expected."""
        return day in self.free_days

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = Path(path or DEFAULT_CONFIG_PATH)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            raw = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

        base = path.parent
        day_cfg = raw.get("working_day") or {}
        cache_cfg = raw.get("cache") or {}
        issues = raw.get("issues") or {}
        sprint_cfg = raw.get("sprint") or {}

        window = Interval(
            parse_hhmm(day_cfg.get("start", "08:00")),
            parse_hhmm(day_cfg.get("end", "18:00")),
        )
        target_minutes = hours_to_minutes(float(day_cfg.get("target_hours", 8.0)))
        if target_minutes > window.duration:
            raise ConfigError("target_hours does not fit inside the working_day window")

        meetings_issue = issues.get("meetings")
        support_issue = issues.get("support")
        if not meetings_issue or not support_issue:
            raise ConfigError(f"Set issues.meetings and issues.support in {path}")

        return cls(
            env_path=(base / raw.get("env_file", "../.env")).resolve(),
            work_window=window,
            target_minutes=target_minutes,
            min_slot_minutes=int(day_cfg.get("min_slot_minutes", 15)),
            cache_path=(base / cache_cfg.get("path", ".cache/worklogs.json")).resolve(),
            cache_ttl_minutes=int(cache_cfg.get("ttl_minutes", 15)),
            meetings_issue=meetings_issue,
            support_issue=support_issue,
            default_project=(issues.get("default_project") or support_issue.split("-")[0]).upper(),
            meeting_blocks=cls._parse_blocks(raw.get("meetings") or [], meetings_issue, window, path),
            free_days=cls._parse_free_days(raw.get("free_days") or [], path),
            sprint_board_id=int(sprint_cfg["board_id"]) if sprint_cfg.get("board_id") is not None else None,
            default_component=issues.get("default_component"),
        )

    @staticmethod
    def _parse_free_days(raw_days: list[str], path: Path) -> frozenset[date]:
        """Holidays/days off, given as ISO 'YYYY-MM-DD' strings in config.toml."""
        try:
            return frozenset(date.fromisoformat(str(day)) for day in raw_days)
        except ValueError as exc:
            raise ConfigError(f"Invalid date in free_days ({path}): {exc}") from exc

    @staticmethod
    def _parse_blocks(
        raw_blocks: list[dict],
        default_issue: str,
        window: Interval,
        path: Path,
    ) -> tuple[MeetingBlock, ...]:
        blocks: list[MeetingBlock] = []
        for raw in raw_blocks:
            name = raw.get("name", "Meeting")
            days = tuple(str(day).lower() for day in raw.get("days", []))
            unknown = [day for day in days if day not in WEEKDAY_KEYS]
            if unknown:
                raise ConfigError(f"Unknown weekday(s) {unknown} in block '{name}' ({path})")
            interval = Interval(parse_hhmm(raw["start"]), parse_hhmm(raw["end"]))
            if interval.start < window.start or interval.end > window.end:
                raise ConfigError(f"Block '{name}' falls outside the working window ({path})")
            blocks.append(
                MeetingBlock(name=name, days=days, interval=interval, issue=raw.get("issue") or default_issue)
            )
        return tuple(blocks)
