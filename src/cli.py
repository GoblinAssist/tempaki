#!/usr/bin/env python3
"""Interactive Tempo week filler.

Usage:
    python3 cli.py                    # plan the current week
    python3 cli.py --week 2026-08-03  # any date inside the target week
    python3 cli.py --day 2026-08-12   # read-only: what is logged on that day
    python3 cli.py --activities 2026-08-12  # read-only: Tempo plans for that day
    python3 cli.py --refresh          # ignore the local cache
    python3 cli.py --dry-run          # never POST to Tempo
    python3 cli.py --clear-cache
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import questionary
from rich import box
from rich.console import Console
from rich.table import Table

from jira_client import ApiError, IssueInfo, JiraClient, TempoClient
from models import Entry, format_hhmm, minutes_to_hours
from settings import AppConfig, ConfigError, Credentials
from time_manager import WeekBoard, WorklogCache, round_to_slot, week_bounds

console = Console()
log = logging.getLogger("tempaki")

DAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")


def label(day: date) -> str:
    return f"{DAY_LABELS[day.weekday()]} {day.isoformat()}"


class Aborted(Exception):
    """The user cancelled a prompt."""


def ask(question) -> object:
    answer = question.ask()
    if answer is None:
        raise Aborted
    return answer


class PlannerApp:
    def __init__(self, config: AppConfig, tempo: TempoClient, jira: JiraClient, dry_run: bool) -> None:
        self._config = config
        self._tempo = tempo
        self._jira = jira
        self._dry_run = dry_run
        self._cache = WorklogCache(config.cache_path, config.cache_ttl_minutes)
        self._board: WeekBoard | None = None
        self._from: date = date.today()
        self._to: date = date.today()

    # -- data ------------------------------------------------------------
    def load_week(self, anchor: date, refresh: bool) -> None:
        self._from, self._to = week_bounds(anchor)
        entries = None if refresh else self._cache.load(self._from, self._to)
        if entries is None:
            console.print("[dim]Fetching worklogs from Tempo...[/dim]")
            entries = self._tempo.fetch_worklogs(self._from, self._to)
            self._cache.save(self._from, self._to, entries)
        else:
            console.print("[dim]Using cached worklogs.[/dim]")
        self._board = WeekBoard(self._config, self._from, entries)

    @property
    def board(self) -> WeekBoard:
        if self._board is None:
            raise RuntimeError("load_week() must be called first")
        return self._board

    # -- phase 1 ---------------------------------------------------------
    def show_summary(self) -> None:
        target = minutes_to_hours(self._config.target_minutes)
        table = Table(
            title=f"Week {self._from} .. {self._to}",
            header_style="bold",
            box=box.SIMPLE_HEAVY,
            show_lines=True,
            pad_edge=False,
        )
        table.add_column("Day", no_wrap=True)
        table.add_column("Date", no_wrap=True)
        table.add_column("Logged", justify="right", no_wrap=True)
        table.add_column("Target", justify="right", no_wrap=True)
        table.add_column("Missing", justify="right", no_wrap=True)
        table.add_column("Entries", overflow="fold")

        for day in self.board.workdays:
            state = self.board.days[day]
            missing = minutes_to_hours(self.board.remaining_minutes(day))
            is_free = self._config.is_free_day(day)
            detail = "\n".join(
                f"{entry.interval}  [cyan]{entry.issue}[/cyan]  {entry.description or '-'}"
                for entry in state.entries
            ) or ("[dim]free day[/dim]" if is_free else "[dim]nothing logged[/dim]")
            table.add_row(
                DAY_LABELS[day.weekday()],
                day.isoformat(),
                f"{state.logged_hours:.2f}h",
                "[dim]--[/dim]" if is_free else f"{target:.2f}h",
                "[green]0.00h[/green]" if missing == 0 else f"[yellow]{missing:.2f}h[/yellow]",
                detail,
            )
        console.print(table)

    def show_day(self, day: date) -> None:
        state = self.board.days.get(day)
        if state is None:
            console.print(f"[yellow]{day} is not a workday (Mon-Fri), nothing is tracked for it.[/yellow]")
            return

        table = Table(title=f"Worklogs for {label(day)}", header_style="bold")
        table.add_column("Slot")
        table.add_column("Hours", justify="right")
        table.add_column("Issue")
        table.add_column("Description")
        table.add_column("Source")

        for entry in state.entries:
            table.add_row(
                str(entry.interval),
                f"{entry.hours:.2f}h",
                entry.issue,
                entry.description or "-",
                "tempo" if entry.synced else "local",
            )
        if not state.entries:
            table.add_row("-", "0.00h", "-", "nothing logged", "-")
        console.print(table)
        console.print(
            f"Total: [bold]{state.logged_hours:.2f}h[/bold] / "
            f"{minutes_to_hours(self._config.target_minutes):.2f}h  "
            f"(missing {minutes_to_hours(self.board.remaining_minutes(day)):.2f}h)"
        )

    def show_planned(self, day: date) -> None:
        """Tempo planned activities (not worklogs) for one day."""
        # Query the whole week: Tempo only returns plans whose range overlaps the request.
        monday, friday = week_bounds(day)
        planned = [item for item in self._tempo.fetch_plans(monday, friday) if item.day == day]
        if not planned:
            console.print(f"[yellow]No planned activities in Tempo for {label(day)}.[/yellow]")
            console.print(
                "[dim]Only Tempo Plans are exposed by the API. Suggestions listed under "
                "'Log Activities' are Tempo Activities and cannot be read; rerun with --verbose "
                "to see what the API returned.[/dim]"
            )
            return

        table = Table(title=f"Planned activities for {label(day)}", header_style="bold", box=box.SIMPLE_HEAVY)
        table.add_column("Start", no_wrap=True)
        table.add_column("Planned", justify="right", no_wrap=True)
        table.add_column("Issue", no_wrap=True)
        table.add_column("Description")

        total = 0
        for item in sorted(planned, key=lambda plan: (plan.start_minutes or 0, plan.issue_key)):
            total += item.minutes
            table.add_row(
                format_hhmm(item.start_minutes) if item.start_minutes is not None else "-",
                f"{minutes_to_hours(item.minutes):.2f}h",
                item.issue_key,
                item.description or "-",
            )
        console.print(table)
        console.print(f"Total planned: [bold]{minutes_to_hours(total):.2f}h[/bold]")

    def show_tasks(self) -> None:
        """List all Jira issues assigned to the authenticated user."""
        issues = self._jira.list_assigned_issues()
        if not issues:
            console.print("[yellow]No Jira issues are assigned to you.[/yellow]")
            return

        table = Table(title=f"Jira issues assigned to you ({len(issues)})", header_style="bold")
        table.add_column("Key", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Summary", overflow="fold")
        for issue in issues:
            table.add_row(issue.key, issue.status, issue.summary)
        console.print(table)

    def show_task_details(self, issue_keys: list[str]) -> None:
        """Show details only for the requested Jira issues."""
        for issue_key in issue_keys:
            issue = self._jira.get_issue_details(issue_key)
            table = Table(title=f"{issue.key}: {issue.summary}", box=box.SIMPLE)
            table.add_column("Field", style="bold")
            table.add_column("Value", overflow="fold")
            for field, value in (
                ("Status", issue.status),
                ("Type", issue.issue_type),
                ("Assignee", issue.assignee),
                ("Reporter", issue.reporter),
                ("Priority", issue.priority),
                ("Created", issue.created),
                ("Updated", issue.updated),
                ("Description", issue.description or "-"),
            ):
                table.add_row(field, value or "-")
            console.print(table)

    # -- phase 2 ---------------------------------------------------------
    def fill_default_meetings(self) -> None:
        pending: dict[date, list[Entry]] = {}
        for day in self.board.workdays:
            entries = self.board.build_meeting_entries(day)
            if entries:
                pending[day] = entries
        if not pending:
            console.print("[green]All default meetings are already logged.[/green]")
            return

        days = ", ".join(DAY_LABELS[day.weekday()] for day in pending)
        console.print(f"\n[bold]Meetings are missing for days:[/bold] {days}")
        table = Table(box=box.SIMPLE, header_style="bold", pad_edge=False)
        table.add_column("Day", no_wrap=True)
        table.add_column("Date", no_wrap=True)
        table.add_column("Slot", no_wrap=True)
        table.add_column("Issue", no_wrap=True)
        table.add_column("Meeting")
        for day, entries in pending.items():
            for entry in entries:
                table.add_row(
                    DAY_LABELS[day.weekday()],
                    day.isoformat(),
                    str(entry.interval),
                    entry.issue,
                    entry.description,
                )
        console.print(table)

        if not ask(questionary.confirm("Add them using the default schedule?", default=True)):
            return
        self._persist([entry for entries in pending.values() for entry in entries])

    # -- planned work from Tempo -----------------------------------------
    def apply_planned_work(self) -> None:
        """Offer the user's Tempo plans as ready-made worklog proposals."""
        console.print("\n[dim]Fetching Tempo plans...[/dim]")
        planned = [
            item for item in self._tempo.fetch_plans(self._from, self._to)
            if item.day in self.board.days
        ]
        if not planned:
            console.print("[dim]No planned activities in Tempo for this week.[/dim]")
            return

        choices = []
        for item in sorted(planned, key=lambda plan: (plan.day, plan.issue_key)):
            remaining = self.board.remaining_minutes(item.day)
            minutes = min(round_to_slot(item.minutes, self._config.min_slot_minutes), remaining)
            if minutes <= 0:
                continue
            capped = " [capped]" if minutes < item.minutes else ""
            choices.append(
                questionary.Choice(
                    title=(
                        f"{DAY_LABELS[item.day.weekday()]} {item.day} "
                        f"{item.issue_key} {minutes_to_hours(minutes):.2f}h{capped} "
                        f"{item.description or ''}".rstrip()
                    ),
                    value=(item, minutes),
                    checked=True,
                )
            )
        if not choices:
            console.print("[dim]Planned activities do not fit - days are already full.[/dim]")
            return

        console.print("[bold]Tempo has planned activities for this week.[/bold]")
        selected = list(ask(questionary.checkbox("Which ones should be logged?", choices=choices)))
        if not selected:
            return

        proposal: list[Entry] = []
        for item, minutes in selected:
            allowed = min(minutes, self.board.remaining_minutes(item.day))
            if allowed <= 0:
                console.print(f"[yellow]{label(item.day)} is already full, skipping {item.issue_key}.[/yellow]")
                continue
            entries, unplaced = self.board.plan_task(
                item.day,
                item.issue_key,
                item.description or item.issue_key,
                allowed,
                preferred_start=item.start_minutes,
            )
            if unplaced:
                console.print(
                    f"[red]{label(item.day)}: {minutes_to_hours(unplaced):.2f}h of {item.issue_key} "
                    f"does not fit in the working window and will be skipped.[/red]"
                )
            if not entries:
                continue
            slots = ", ".join(str(entry.interval) for entry in entries)
            console.print(f"{label(item.day)}  {item.issue_key}  [bold]{slots}[/bold]")
            # Committed immediately so the next item sees these slots as busy.
            self.board.commit(entries)
            proposal.extend(entries)

        if not proposal:
            return
        if not ask(questionary.confirm("Accept this schedule?", default=True)):
            self._rollback(proposal)
            return
        self._persist(proposal, already_on_board=True)

    # -- phases 3-5 ------------------------------------------------------
    def fill_custom_gaps(self) -> None:
        while True:
            incomplete = self.board.incomplete_days()
            if not incomplete:
                console.print("\n[bold green]All workdays reach the daily target.[/bold green]")
                return

            days = ", ".join(DAY_LABELS[day.weekday()] for day in incomplete)
            console.print(f"\n[bold]There is still time to organize for days:[/bold] {days}")
            if not ask(questionary.confirm("Do you want to add custom workload?", default=True)):
                return

            issue = self._ask_issue()
            description = str(ask(questionary.text("Worklog description:", default=issue.summary[:80]))).strip()
            chosen = self._ask_days(incomplete)
            if not chosen:
                continue
            requested_minutes = self._ask_minutes()

            planned = self._plan_days(issue.key, description or issue.key, chosen, requested_minutes)
            if planned:
                self._persist(planned)

    def _ask_issue(self) -> IssueInfo:
        project = self._config.default_project
        while True:
            raw = str(
                ask(
                    questionary.text(
                        f"Jira task ID (e.g. {self._config.support_issue}):",
                        default=f"{project}-",
                    )
                )
            ).strip()
            # A bare number is completed with the configured project prefix.
            key = f"{project}-{raw}" if raw.isdigit() else raw
            if not key or key == f"{project}-":
                continue
            try:
                issue = self._jira.get_issue(key)
            except ApiError as exc:
                console.print(f"[red]{exc}[/red]")
                continue
            console.print(f"  [cyan]{issue.key}[/cyan] {issue.summary} [dim](status: {issue.status})[/dim]")
            if not issue.is_open and not ask(
                questionary.confirm(f"{issue.key} is closed. Use it anyway?", default=False)
            ):
                continue
            return issue

    def _ask_days(self, incomplete: list[date]) -> list[date]:
        choices = [
            questionary.Choice(
                title=f"{label(day)} (missing {minutes_to_hours(self.board.remaining_minutes(day)):.2f}h)",
                value=day,
            )
            for day in incomplete
        ]
        return list(ask(questionary.checkbox("Apply to which days?", choices=choices)))

    def _ask_minutes(self) -> int:
        def validate(text: str) -> bool | str:
            try:
                value = float(text.replace(",", "."))
            except ValueError:
                return "Enter a number, e.g. 2.5"
            return "Must be greater than 0" if value <= 0 else True

        hours = float(str(ask(questionary.text("Hours per day:", validate=validate))).replace(",", "."))
        minutes = round_to_slot(round(hours * 60), self._config.min_slot_minutes)
        if minutes != round(hours * 60):
            console.print(f"[dim]Rounded to {minutes_to_hours(minutes):.2f}h ({self._config.min_slot_minutes}min steps).[/dim]")
        return minutes

    def _plan_days(
        self,
        issue_key: str,
        description: str,
        days: list[date],
        requested_minutes: int,
    ) -> list[Entry]:
        proposal: list[Entry] = []
        for day in days:
            remaining = self.board.remaining_minutes(day)
            minutes = requested_minutes
            if minutes > remaining:
                console.print(
                    f"[yellow]{label(day)}: {minutes_to_hours(minutes):.2f}h would exceed the daily target; "
                    f"capping to {minutes_to_hours(remaining):.2f}h.[/yellow]"
                )
                if not ask(questionary.confirm(f"Cap {DAY_LABELS[day.weekday()]} to the remaining time?", default=True)):
                    continue
                minutes = remaining
            if minutes <= 0:
                continue

            entries, unplaced = self.board.plan_task(day, issue_key, description, minutes)
            if unplaced:
                console.print(
                    f"[red]{label(day)}: {minutes_to_hours(unplaced):.2f}h does not fit before "
                    f"the end of the working window and will be skipped.[/red]"
                )
            if not entries:
                continue
            slots = ", ".join(str(entry.interval) for entry in entries)
            console.print(f"Proposed schedule for {issue_key} on {label(day)}: [bold]{slots}[/bold]")
            proposal.extend(entries)

        if not proposal:
            return []
        if not ask(questionary.confirm("Accept this schedule?", default=True)):
            return []
        return proposal

    # -- orchestration ---------------------------------------------------
    def run_week(self, anchor: date, refresh: bool) -> None:
        self.load_week(anchor, refresh=refresh)
        self.show_summary()
        self.fill_default_meetings()
        self.apply_planned_work()
        self.fill_custom_gaps()
        self.show_summary()

    def ask_next_week(self) -> date | None:
        """Offer another week to work on; None ends the session."""
        next_monday = self._from + timedelta(days=7)
        previous_monday = self._from - timedelta(days=7)
        choice = ask(
            questionary.select(
                "Anything else?",
                choices=[
                    questionary.Choice(f"Next week ({next_monday})", value=next_monday),
                    questionary.Choice(f"Previous week ({previous_monday})", value=previous_monday),
                    questionary.Choice("Another week (enter a date)", value="custom"),
                    questionary.Choice("Done", value="done"),
                ],
            )
        )
        if choice == "done":
            return None
        if choice != "custom":
            return choice
        while True:
            raw = str(ask(questionary.text("Any date inside that week (YYYY-MM-DD):"))).strip()
            try:
                return datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                console.print("[red]Expected format YYYY-MM-DD[/red]")

    # -- persistence -----------------------------------------------------
    def _rollback(self, entries: list[Entry]) -> None:
        self.board.rollback(entries)

    def _persist(self, entries: list[Entry], already_on_board: bool = False) -> None:
        if already_on_board:
            self.board.rollback(entries)
        saved: list[Entry] = []
        for entry in entries:
            if self._dry_run:
                console.print(f"[dim]dry-run:[/dim] {label(entry.day)} {entry.interval} {entry.issue}")
                saved.append(entry)
                continue
            try:
                worklog_id = self._tempo.create_worklog(entry)
            except ApiError as exc:
                console.print(f"[red]Failed to log {entry.issue} on {entry.day} {entry.interval}: {exc}[/red]")
                continue
            console.print(f"[green]Logged[/green] {entry.hours:.2f}h {entry.issue} {label(entry.day)} {entry.interval}")
            saved.append(entry.mark_synced(worklog_id))

        # Local cache is updated straight away so the next iteration/run is accurate.
        self.board.commit(saved)
        self._cache.save(self._from, self._to, self.board.all_entries())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill Tempo workdays interactively.")
    parser.add_argument("--week", help="Any date (YYYY-MM-DD) inside the target week; defaults to today")
    parser.add_argument("--day", help="Show what is logged on this date (YYYY-MM-DD) and exit")
    parser.add_argument("--activities", help="Show Tempo planned activities for this date (YYYY-MM-DD) and exit")
    task_args = parser.add_mutually_exclusive_group()
    task_args.add_argument("--tasks", action="store_true", help="List all Jira issues assigned to you and exit")
    task_args.add_argument("--task", nargs="+", metavar="ISSUE_KEY", help="Show details for specified Jira issue(s) and exit")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--refresh", action="store_true", help="Ignore the local cache")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be logged, POST nothing")
    parser.add_argument("--clear-cache", action="store_true", help="Delete the cache file and exit")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    try:
        config = AppConfig.load(args.config)
        if args.clear_cache:
            WorklogCache(config.cache_path, config.cache_ttl_minutes).clear()
            console.print("Cache cleared.")
            return 0

        target_day = datetime.strptime(args.day, "%Y-%m-%d").date() if args.day else None
        credentials = Credentials.from_env(config.env_path)
        jira = JiraClient(credentials)
        tempo = TempoClient(credentials, jira)

        app = PlannerApp(config, tempo, jira, dry_run=args.dry_run)
        if args.activities:
            app.show_planned(datetime.strptime(args.activities, "%Y-%m-%d").date())
            return 0
        if args.tasks:
            app.show_tasks()
            return 0
        if args.task:
            app.show_task_details(args.task)
            return 0
        if target_day:
            app.load_week(target_day, refresh=args.refresh)
            app.show_day(target_day)
            return 0

        anchor: date | None = (
            datetime.strptime(args.week, "%Y-%m-%d").date() if args.week else date.today()
        )
        while anchor is not None:
            app.run_week(anchor, refresh=args.refresh)
            anchor = app.ask_next_week()
    except ValueError as exc:
        console.print(f"[red]Invalid input: {exc}[/red]")
        return 2
    except (ConfigError, ApiError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except (Aborted, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
