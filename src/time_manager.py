"""Time-slot arithmetic, gap filling and the local worklog cache.

This module is pure logic plus file I/O - it never talks to Jira or Tempo,
which keeps the splitting algorithm trivially testable.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from models import DayState, Entry, Interval, MeetingBlock
from settings import AppConfig

log = logging.getLogger(__name__)

WORKDAYS_PER_WEEK = 5


# ---------------------------------------------------------------------------
# Interval algebra
# ---------------------------------------------------------------------------
def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Collapse overlapping/touching intervals into a minimal sorted list."""
    merged: list[Interval] = []
    for interval in sorted(intervals):
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, interval.end)
        else:
            merged.append(interval)
    return merged


def free_intervals(window: Interval, busy: Iterable[Interval]) -> list[Interval]:
    """Gaps inside `window` that are not covered by `busy`."""
    gaps: list[Interval] = []
    cursor = window.start
    for block in merge_intervals(busy):
        if block.end <= window.start or block.start >= window.end:
            continue
        start = max(block.start, window.start)
        if start > cursor:
            gaps.append(Interval(cursor, start))
        cursor = max(cursor, min(block.end, window.end))
    if cursor < window.end:
        gaps.append(Interval(cursor, window.end))
    return gaps


def round_to_slot(minutes: int, slot_minutes: int) -> int:
    """Nearest multiple of `slot_minutes` (never below one slot for positive input)."""
    if minutes <= 0:
        return 0
    return max(round(minutes / slot_minutes) * slot_minutes, slot_minutes)


def _ceil_to(value: int, step: int) -> int:
    return -(-value // step) * step


def _floor_to(value: int, step: int) -> int:
    return (value // step) * step


def allocate_slots(
    gaps: Sequence[Interval],
    minutes_needed: int,
    slot_minutes: int = 15,
) -> tuple[list[Interval], int]:
    """Fill `minutes_needed` into `gaps`, earliest first, splitting as required.

    Every slot starts and ends on a `slot_minutes` boundary. Returns the
    allocated slots and the minutes that did not fit (the 18:00 hard stop).
    """
    slots: list[Interval] = []
    remaining = round_to_slot(minutes_needed, slot_minutes)
    for gap in gaps:
        if remaining <= 0:
            break
        start = _ceil_to(gap.start, slot_minutes)
        available = _floor_to(gap.end - start, slot_minutes) if start < gap.end else 0
        take = min(available, remaining)
        if take <= 0:
            continue
        slots.append(Interval(start, start + take))
        remaining -= take
    return slots, max(remaining, 0)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
class WorklogCache:
    """JSON-backed cache of the entries for one date range.

    Locally planned entries are written here immediately, so a re-run picks up
    the new state even before Tempo has been re-queried.
    """

    def __init__(self, path: Path, ttl_minutes: int) -> None:
        self._path = path
        self._ttl_seconds = ttl_minutes * 60

    def load(self, from_date: date, to_date: date) -> list[Entry] | None:
        if not self._path.exists():
            return None
        try:
            payload = json.loads(self._path.read_text())
        except ValueError:
            log.warning("Cache file %s is corrupted, ignoring it", self._path)
            return None
        if payload.get("from") != from_date.isoformat() or payload.get("to") != to_date.isoformat():
            return None
        if time.time() - float(payload.get("fetched_at", 0)) > self._ttl_seconds:
            return None
        return [Entry.from_dict(item) for item in payload.get("entries", [])]

    def save(self, from_date: date, to_date: date, entries: Iterable[Entry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "fetched_at": time.time(),
            "entries": [entry.to_dict() for entry in entries],
        }
        self._path.write_text(json.dumps(payload, indent=2))

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Week planning
# ---------------------------------------------------------------------------
def week_bounds(anchor: date) -> tuple[date, date]:
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=WORKDAYS_PER_WEEK - 1)


class WeekBoard:
    """Mutable Mon-Fri view of what is logged and what is still missing."""

    def __init__(self, config: AppConfig, monday: date, entries: Iterable[Entry]) -> None:
        self._config = config
        self.monday = monday
        self.friday = monday + timedelta(days=WORKDAYS_PER_WEEK - 1)
        self.days: dict[date, DayState] = {
            monday + timedelta(days=offset): DayState(monday + timedelta(days=offset))
            for offset in range(WORKDAYS_PER_WEEK)
        }
        for entry in entries:
            if entry.day in self.days:
                self.days[entry.day].add([entry])

    @property
    def workdays(self) -> list[date]:
        return sorted(self.days)

    def all_entries(self) -> list[Entry]:
        return [entry for day in self.workdays for entry in self.days[day].entries]

    def remaining_minutes(self, day: date) -> int:
        """Missing time, snapped down to the slot grid so proposals stay round."""
        if self._config.is_free_day(day):
            return 0
        missing = max(self._config.target_minutes - self.days[day].logged_minutes, 0)
        return _floor_to(missing, self._config.min_slot_minutes)

    def incomplete_days(self) -> list[date]:
        return [day for day in self.workdays if self.remaining_minutes(day) > 0]

    def missing_meetings(self, day: date) -> list[MeetingBlock]:
        """Configured blocks for `day` that nothing currently overlaps."""
        if self._config.is_free_day(day):
            return []
        busy = self.days[day].busy()
        return [
            block for block in self._config.blocks_for(day)
            if not any(block.interval.overlaps(existing) for existing in busy)
        ]

    def build_meeting_entries(self, day: date) -> list[Entry]:
        return [
            Entry(day=day, issue=block.issue, description=block.name, interval=block.interval)
            for block in self.missing_meetings(day)
        ]

    def plan_task(
        self,
        day: date,
        issue: str,
        description: str,
        minutes: int,
        preferred_start: int | None = None,
    ) -> tuple[list[Entry], int]:
        """Split `minutes` of work around everything already booked on `day`."""
        gaps = free_intervals(self._config.work_window, self.days[day].busy())
        if preferred_start is not None:
            # Start at the planned time when possible, fall back to earlier gaps.
            gaps = [gap for gap in gaps if gap.end > preferred_start] + [
                gap for gap in gaps if gap.end <= preferred_start
            ]
            gaps = [
                Interval(max(gap.start, preferred_start), gap.end)
                if gap.start < preferred_start < gap.end
                else gap
                for gap in gaps
            ]
        slots, unplaced = allocate_slots(gaps, minutes, self._config.min_slot_minutes)
        entries = [
            Entry(day=day, issue=issue, description=description, interval=slot) for slot in slots
        ]
        return entries, unplaced

    def commit(self, entries: Iterable[Entry]) -> None:
        for entry in entries:
            self.days[entry.day].add([entry])

    def rollback(self, entries: Iterable[Entry]) -> None:
        """Drop provisional entries that were committed but not accepted."""
        for entry in entries:
            state = self.days.get(entry.day)
            if state and entry in state.entries:
                state.entries.remove(entry)
