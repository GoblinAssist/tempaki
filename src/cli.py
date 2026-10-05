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
    python3 cli.py --create-task --project PROJ --summary "..." --description "..." --type Task
    python3 cli.py --create-task --project PROJ --summary "..." --sprint  # also add to the active sprint
    python3 cli.py --create-task --project PROJ --summary "..." --assignee none  # created unassigned (default: assigned to you)
    python3 cli.py --update PROJ-123 --status "In Progress"
    python3 cli.py --update PROJ-123 --assignee me
    python3 cli.py --update PROJ-123 --parent PROJ-100
    python3 cli.py --assignable PROJ-123
    python3 cli.py --list-components PROJ
    python3 cli.py --update PROJ-123 --components Component1
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

    def show_assignable_users(self, issue_key: str | None = None, project_key: str | None = None) -> None:
        """List Jira accounts that can be assigned to this issue or project."""
        users = self._jira.list_assignable_users(project_key=project_key, issue_key=issue_key)
        label = issue_key.strip().upper() if issue_key else project_key.strip().upper()
        if not users:
            console.print(f"[yellow]No assignable users found for {label}.[/yellow]")
            return
        table = Table(title=f"Assignable accounts for {label} ({len(users)})", header_style="bold")
        table.add_column("Account ID", no_wrap=True)
        table.add_column("Display name")
        table.add_column("Email", overflow="fold")
        for user in users:
            table.add_row(user.get("accountId", "-"), user.get("displayName", "-"), user.get("emailAddress") or "-")
        console.print(table)

    def show_components(self, project_key: str) -> None:
        """List the components configured for a Jira project."""
        components = self._jira.list_components(project_key)
        if not components:
            console.print(f"[yellow]No components found for project {project_key.strip().upper()}.[/yellow]")
            return
        table = Table(title=f"Components for {project_key.strip().upper()} ({len(components)})", header_style="bold")
        table.add_column("ID", no_wrap=True)
        table.add_column("Name")
        for component in components:
            table.add_row(component.get("id", "-"), component.get("name", "-"))
        console.print(table)

    def _resolve_assignee(
        self,
        assignee: str | None,
        project_key: str | None = None,
        issue_key: str | None = None,
    ) -> str | None:
        """Turn 'me'/None/a name-or-email into an accountId verified as assignable, or None.

        Verifies the account directly against Jira's assignable-users check for
        `project_key`/`issue_key` (rather than a possibly-truncated listing)
        before returning it, so we never send an accountId Jira would reject.
        """
        if not assignee or assignee.casefold() in {"none", "unassigned"}:
            return None
        if assignee.casefold() == "me":
            account_id = self._jira.account_id()
            if not self._jira.is_assignable(account_id, project_key=project_key, issue_key=issue_key):
                raise ApiError(f"You ({account_id}) are not assignable to this issue/project")
            return account_id
        return self._jira.find_account_id(assignee, project_key=project_key, issue_key=issue_key)

    def create_task(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        add_to_sprint: bool = False,
        assignee: str | None = "me",
        parent_key: str | None = None,
        components: list[str] | None = None,
    ) -> None:
        """Create a new Jira issue, optionally placing it in the current active sprint."""
        account_id = self._resolve_assignee(assignee, project_key=project_key)
        components = components or ([self._config.default_component] if self._config.default_component else None)
        resolved_components = (
            self._jira.resolve_component_names(project_key, components) if components else None
        )
        issue = self._jira.create_issue(
            project_key=project_key,
            summary=summary,
            description=description,
            issue_type=issue_type,
            assignee_account_id=account_id,
            component_names=resolved_components,
        )
        console.print(f"[green]Created {issue.key}[/green]: {issue.summary}")

        # Many "Create Issue" screens omit the Assignee field, so Jira can silently
        # drop it from the creation payload; assign explicitly as a follow-up to
        # guarantee it actually takes (mirrors how parent/components are applied).
        if account_id:
            self._jira.assign_issue(issue.key, account_id, verify=False)
            console.print(f"[green]{issue.key} assignee set to[/green]: {assignee}")

        if resolved_components:
            console.print(f"[green]{issue.key} components set to[/green]: {', '.join(resolved_components)}")

        if parent_key:
            self._jira.set_parent(issue.key, parent_key)
            console.print(f"[green]{issue.key} parent set to[/green]: {parent_key.strip().upper()}")

        if not add_to_sprint:
            return
        if self._config.sprint_board_id is None:
            console.print("[yellow]--sprint requested but no [sprint] board_id is set in config.toml; issue left in backlog.[/yellow]")
            return
        sprint = self._jira.get_active_sprint(self._config.sprint_board_id)
        if sprint is None:
            console.print(f"[yellow]No active sprint on board {self._config.sprint_board_id}; issue left in backlog.[/yellow]")
            return
        self._jira.add_issue_to_sprint(sprint["id"], issue.key)
        console.print(f"[green]Added {issue.key} to current sprint[/green]: {sprint['name']}")

    def update_task(
        self,
        issue_key: str,
        status: str | None = None,
        assignee: str | None = None,
        parent_key: str | None = None,
        components: list[str] | None = None,
    ) -> None:
        """Update an existing issue's workflow status, assignee, parent, and/or components."""
        if status is None and assignee is None and parent_key is None and components is None:
            console.print("[red]--update requires --status, --assignee, --parent, and/or --components[/red]")
            return
        if status is not None:
            self._jira.transition_issue(issue_key, status)
            console.print(f"[green]{issue_key} moved to status[/green]: {status}")
        if assignee is not None:
            account_id = self._resolve_assignee(assignee, issue_key=issue_key)
            self._jira.assign_issue(issue_key, account_id)
            console.print(f"[green]{issue_key} assignee updated[/green]: {assignee}")
        if parent_key is not None:
            self._jira.set_parent(issue_key, parent_key)
            console.print(f"[green]{issue_key} parent set to[/green]: {parent_key.strip().upper()}")
        if components is not None:
            project_key = issue_key.strip().upper().split("-")[0]
            resolved_components = self._jira.resolve_component_names(project_key, components)
            self._jira.set_components(issue_key, resolved_components)
            console.print(f"[green]{issue_key} components set to[/green]: {', '.join(resolved_components)}")

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
                ("Parent", issue.parent),
                ("Components", issue.components),
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

    # -- non-interactive logging ------------------------------------------
    def _resolve_issue_key(self, raw: str) -> str:
        raw = raw.strip()
        # A bare number is completed with the configured project prefix.
        return f"{self._config.default_project}-{raw}" if raw.isdigit() else raw.upper()

    def _parse_log_entry(self, spec: str) -> tuple[str, float, str]:
        parts = spec.split(":", 2)
        if len(parts) < 2:
            raise ValueError(f"Invalid --log entry {spec!r}; expected ISSUE:HOURS[:DESCRIPTION]")
        issue_key = self._resolve_issue_key(parts[0])
        try:
            hours = float(parts[1])
        except ValueError as exc:
            raise ValueError(f"Invalid hours in --log entry {spec!r}: {parts[1]!r}") from exc
        description = parts[2].strip() if len(parts) == 3 and parts[2].strip() else self._jira.get_issue(issue_key).summary
        return issue_key, hours, description

    def log_day(
        self,
        day: date,
        use_default_meetings: bool,
        manual_specs: list[str],
        split_rest: list[str],
    ) -> None:
        """Non-interactively log worklogs for a single day and persist them."""
        entries: list[Entry] = []

        if use_default_meetings:
            meeting_entries = self.board.build_meeting_entries(day)
            if meeting_entries:
                self.board.commit(meeting_entries)
                entries.extend(meeting_entries)
                slots = ", ".join(f"{entry.interval} {entry.issue}" for entry in meeting_entries)
                console.print(f"[dim]{label(day)}: default meetings added -[/dim] {slots}")
            else:
                console.print(f"[dim]{label(day)}: no default meetings missing.[/dim]")

        for spec in manual_specs:
            issue_key, hours, description = self._parse_log_entry(spec)
            minutes = round_to_slot(round(hours * 60), self._config.min_slot_minutes)
            remaining = self.board.remaining_minutes(day)
            allowed = min(minutes, remaining)
            if allowed <= 0:
                console.print(f"[yellow]{label(day)} is already full, skipping {issue_key}.[/yellow]")
                continue
            planned, unplaced = self.board.plan_task(day, issue_key, description, allowed)
            if unplaced:
                console.print(
                    f"[red]{label(day)}: {minutes_to_hours(unplaced):.2f}h of {issue_key} "
                    f"does not fit in the working window and will be skipped.[/red]"
                )
            if not planned:
                continue
            self.board.commit(planned)
            entries.extend(planned)
            slots = ", ".join(str(entry.interval) for entry in planned)
            console.print(f"{label(day)}  {issue_key}  [bold]{slots}[/bold]")

        if split_rest:
            resolved = [self._resolve_issue_key(key) for key in split_rest]
            remaining = self.board.remaining_minutes(day)
            if remaining <= 0:
                console.print(f"[yellow]{label(day)}: no remaining time left to split.[/yellow]")
            else:
                count = len(resolved)
                base, extra = divmod(remaining, count)
                shares = [base + (1 if i < extra else 0) for i in range(count)]
                for issue_key, share_minutes in zip(resolved, shares):
                    minutes = round_to_slot(share_minutes, self._config.min_slot_minutes)
                    remaining_now = self.board.remaining_minutes(day)
                    allowed = min(minutes, remaining_now)
                    if allowed <= 0:
                        continue
                    description = self._jira.get_issue(issue_key).summary
                    planned, unplaced = self.board.plan_task(day, issue_key, description, allowed)
                    if unplaced:
                        console.print(
                            f"[red]{label(day)}: {minutes_to_hours(unplaced):.2f}h of {issue_key} "
                            f"does not fit in the working window and will be skipped.[/red]"
                        )
                    if not planned:
                        continue
                    self.board.commit(planned)
                    entries.extend(planned)
                    slots = ", ".join(str(entry.interval) for entry in planned)
                    console.print(f"{label(day)}  {issue_key}  [bold]{slots}[/bold]")

        if not entries:
            console.print("[yellow]Nothing to log.[/yellow]")
            return
        self._persist(entries, already_on_board=True)

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
    task_args.add_argument("--create-task", action="store_true", help="Create a new Jira issue and exit")
    task_args.add_argument("--update", metavar="ISSUE_KEY", help="Update an existing issue's status/assignee and exit")
    task_args.add_argument("--assignable", metavar="ISSUE_KEY", help="List accounts assignable to this issue and exit")
    task_args.add_argument("--assignable-project", metavar="PROJECT_KEY", help="List accounts assignable in this project and exit")
    task_args.add_argument("--list-components", metavar="PROJECT_KEY", help="List components configured for this project and exit")
    parser.add_argument("--project", metavar="KEY", help="Jira project key (required with --create-task)")
    parser.add_argument("--summary", help="Summary for the new issue (required with --create-task)")
    parser.add_argument("--description", default="", help="Description for the new issue")
    parser.add_argument("--type", dest="issue_type", default="Task", help="Issue type for the new issue (default: Task)")
    parser.add_argument("--sprint", action="store_true", help="With --create-task, add the new issue to the current active sprint (requires [sprint] board_id in config.toml)")
    parser.add_argument(
        "--assignee",
        default=None,
        help="Assignee for --create-task (default: 'me') or --update: 'me', 'none'/'unassigned', or a Jira name/email",
    )
    parser.add_argument("--status", help="With --update, transition the issue to this workflow status")
    parser.add_argument("--parent", help="With --create-task/--update, set the parent issue key")
    parser.add_argument(
        "--components",
        nargs="+",
        metavar="NAME",
        help="With --create-task/--update, set issue component(s) by name (matched against the project's real components)",
    )
    parser.add_argument("--log-day", help="Target date (YYYY-MM-DD) for --log/--default-meetings/--split-rest; defaults to today")
    parser.add_argument(
        "--default-meetings",
        action="store_true",
        help="Non-interactively log the default meeting schedule for --log-day",
    )
    parser.add_argument(
        "--log",
        nargs="+",
        metavar="ISSUE:HOURS[:DESCRIPTION]",
        help="Non-interactively log specific durations, e.g. PROJ-999:0.5:Support call",
    )
    parser.add_argument(
        "--split-rest",
        nargs="+",
        metavar="ISSUE_KEY",
        help="After --default-meetings/--log, split the day's remaining time evenly across these issues",
    )
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
        if args.create_task:
            if not args.project or not args.summary:
                console.print("[red]--create-task requires --project and --summary[/red]")
                return 2
            app.create_task(
                args.project,
                args.summary,
                args.description,
                args.issue_type,
                add_to_sprint=args.sprint,
                assignee=args.assignee or "me",
                parent_key=args.parent,
                components=args.components,
            )
            return 0
        if args.update:
            app.update_task(
                args.update,
                status=args.status,
                assignee=args.assignee,
                parent_key=args.parent,
                components=args.components,
            )
            return 0
        if args.assignable:
            app.show_assignable_users(issue_key=args.assignable)
            return 0
        if args.assignable_project:
            app.show_assignable_users(project_key=args.assignable_project)
            return 0
        if args.list_components:
            app.show_components(args.list_components)
            return 0
        if args.default_meetings or args.log or args.split_rest:
            log_day = (
                datetime.strptime(args.log_day, "%Y-%m-%d").date() if args.log_day else date.today()
            )
            app.load_week(log_day, refresh=args.refresh)
            app.log_day(
                log_day,
                use_default_meetings=args.default_meetings,
                manual_specs=args.log or [],
                split_rest=args.split_rest or [],
            )
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
