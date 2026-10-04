"""Domain model: time intervals, worklog entries and per-day state.

Times are represented as *minutes since midnight* (int) everywhere inside the
application; conversion to/from "HH:MM[:SS]" happens only at the edges.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_hhmm(value: str) -> int:
    """'10:30' or '10:30:00' -> minutes since midnight."""
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid time {value!r}, expected HH:MM")
    hours, minutes = int(parts[0]), int(parts[1])
    if not 0 <= hours <= 24 or not 0 <= minutes < 60:
        raise ValueError(f"Invalid time {value!r}")
    return hours * 60 + minutes


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_hhmmss(minutes: int) -> str:
    return f"{format_hhmm(minutes)}:00"


def hours_to_minutes(hours: float) -> int:
    return int(round(hours * 60))


def minutes_to_hours(minutes: int) -> float:
    return round(minutes / 60.0, 2)


@dataclass(frozen=True, order=True)
class Interval:
    """Half-open time range [start, end) in minutes since midnight."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(f"Empty or reversed interval {self.start}-{self.end}")

    @property
    def duration(self) -> int:
        return self.end - self.start

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def __str__(self) -> str:
        return f"{format_hhmm(self.start)}-{format_hhmm(self.end)}"


@dataclass(frozen=True)
class MeetingBlock:
    """A recurring, fixed-time activity defined in config.toml."""

    name: str
    days: tuple[str, ...]
    interval: Interval
    issue: str

    def applies_to(self, day: date) -> bool:
        return WEEKDAY_KEYS[day.weekday()] in self.days


@dataclass(frozen=True)
class Entry:
    """A single worklog occupying one time slot on one day."""

    day: date
    issue: str
    description: str
    interval: Interval
    tempo_worklog_id: int | None = None
    synced: bool = False

    @property
    def hours(self) -> float:
        return minutes_to_hours(self.interval.duration)

    def to_dict(self) -> dict:
        return {
            "date": self.day.isoformat(),
            "issue": self.issue,
            "description": self.description,
            "start": format_hhmm(self.interval.start),
            "minutes": self.interval.duration,
            "tempo_worklog_id": self.tempo_worklog_id,
            "synced": self.synced,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Entry":
        start = parse_hhmm(payload["start"])
        return cls(
            day=date.fromisoformat(payload["date"]),
            issue=payload["issue"],
            description=payload.get("description", ""),
            interval=Interval(start, start + int(payload["minutes"])),
            tempo_worklog_id=payload.get("tempo_worklog_id"),
            synced=bool(payload.get("synced", False)),
        )

    def mark_synced(self, worklog_id: int | None) -> "Entry":
        return replace(self, tempo_worklog_id=worklog_id, synced=True)


@dataclass
class DayState:
    """Everything already logged (remotely or locally) for one calendar day."""

    day: date
    entries: list[Entry] = field(default_factory=list)

    @property
    def logged_minutes(self) -> int:
        return sum(entry.interval.duration for entry in self.entries)

    @property
    def logged_hours(self) -> float:
        return minutes_to_hours(self.logged_minutes)

    @property
    def weekday_key(self) -> str:
        return WEEKDAY_KEYS[self.day.weekday()]

    def busy(self) -> list[Interval]:
        return sorted(entry.interval for entry in self.entries)

    def add(self, entries: Iterable[Entry]) -> None:
        self.entries.extend(entries)
        self.entries.sort(key=lambda entry: entry.interval)
