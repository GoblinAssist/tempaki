"""HTTP access to Jira Cloud and Tempo, with bounded retries.

Only these classes know about the wire format; the rest of the app works with
the domain model from models.py.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests
from requests.auth import HTTPBasicAuth

from models import Entry, Interval, parse_hhmm, format_hhmmss
from settings import Credentials

log = logging.getLogger(__name__)

TEMPO_BASE_URL = "https://api.tempo.io/4"
PAGE_SIZE = 1000
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class ApiError(RuntimeError):
    """Any non-recoverable Jira/Tempo API failure."""


@dataclass(frozen=True)
class IssueInfo:
    key: str
    issue_id: int
    summary: str
    status: str
    is_open: bool


@dataclass(frozen=True)
class IssueDetails:
    key: str
    summary: str
    status: str
    issue_type: str
    assignee: str
    reporter: str
    priority: str
    created: str
    updated: str
    description: str
    parent: str
    components: str


@dataclass(frozen=True)
class PlannedWork:
    """One day's worth of a Tempo plan (capacity planned, not yet logged)."""

    issue_key: str
    description: str
    day: date
    minutes: int
    start_minutes: int | None = None


class RetryingSession:
    """requests.Session wrapper with exponential backoff on transient errors."""

    def __init__(self, max_attempts: int = 4, backoff_seconds: float = 1.0, timeout: int = 30) -> None:
        self._session = requests.Session()
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._timeout = timeout

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        last_error: str = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._session.request(method, url, timeout=self._timeout, **kwargs)
            except requests.RequestException as exc:
                last_error = f"network error: {exc}"
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"

            if attempt == self._max_attempts:
                break
            delay = self._backoff * (2 ** (attempt - 1))
            log.warning("%s %s failed (%s), retrying in %.1fs", method, url, last_error, delay)
            time.sleep(delay)
        raise ApiError(f"{method} {url} failed after {self._max_attempts} attempts - {last_error}")


class JiraClient:
    """Issue and user lookups (Tempo needs numeric issue ids and account ids)."""

    def __init__(self, credentials: Credentials, session: RetryingSession | None = None) -> None:
        self._credentials = credentials
        self._session = session or RetryingSession()
        self._auth = HTTPBasicAuth(credentials.jira_user, credentials.jira_token)
        self._account_id: str | None = None
        self._issue_cache: dict[str, IssueInfo] = {}
        self._key_by_id: dict[int, str] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        response = self._session.request(
            "GET",
            f"{self._credentials.jira_url}{path}",
            params=params,
            auth=self._auth,
            headers={"Accept": "application/json"},
        )
        if response.status_code == 404:
            raise ApiError(f"Not found: {path}")
        if response.status_code >= 300:
            raise ApiError(f"Jira API error ({response.status_code}): {response.text[:300]}")
        return response.json()

    def _write(self, method: str, path: str, body: dict) -> dict:
        response = self._session.request(
            method,
            f"{self._credentials.jira_url}{path}",
            json=body,
            auth=self._auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        if response.status_code >= 300:
            raise ApiError(f"Jira API error ({response.status_code}): {response.text[:300]}")
        return response.json() if response.content else {}

    def account_id(self) -> str:
        if self._account_id is None:
            self._account_id = self._get("/rest/api/3/myself")["accountId"]
        return self._account_id

    def get_issue(self, issue_key: str) -> IssueInfo:
        """Resolve and validate an issue key. Raises ApiError if it does not exist."""
        key = issue_key.strip().upper()
        if key not in self._issue_cache:
            self._remember(self._get(f"/rest/api/3/issue/{key}", params={"fields": "summary,status"}))
        return self._issue_cache[key]

    def get_issue_details(self, issue_key: str) -> IssueDetails:
        """Fetch display details for one Jira issue."""
        key = issue_key.strip().upper()
        fields = self._get(
            f"/rest/api/3/issue/{key}",
            params={"fields": "summary,status,description,assignee,reporter,issuetype,priority,created,updated,parent,components"},
        ).get("fields", {})
        status = fields.get("status") or {}
        issue_type = fields.get("issuetype") or {}
        assignee = fields.get("assignee") or {}
        reporter = fields.get("reporter") or {}
        priority = fields.get("priority") or {}
        parent = fields.get("parent") or {}
        components = fields.get("components") or []
        return IssueDetails(
            key=key,
            summary=fields.get("summary", ""),
            status=status.get("name", "unknown"),
            issue_type=issue_type.get("name", "unknown"),
            assignee=assignee.get("displayName", "Unassigned"),
            reporter=reporter.get("displayName", "Unknown"),
            priority=priority.get("name", "None"),
            created=fields.get("created", ""),
            updated=fields.get("updated", ""),
            description=self._adf_to_text(fields.get("description")),
            parent=parent.get("key", "None"),
            components=", ".join(c.get("name", "") for c in components) or "None",
        )

    def create_issue(
        self,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        assignee_account_id: str | None = None,
        component_names: list[str] | None = None,
    ) -> IssueInfo:
        """Create a Jira issue and return its resolved details."""
        fields = {
            "project": {"key": project_key.strip().upper()},
            "issuetype": {"name": issue_type},
            "summary": summary.strip(),
        }
        if description:
            fields["description"] = self._description_document(description)
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if component_names:
            fields["components"] = [{"name": name} for name in component_names]
        created = self._write("POST", "/rest/api/3/issue", {"fields": fields})
        return self.get_issue(created["key"])

    def list_components(self, project_key: str) -> list[dict]:
        """List the components configured for a project (id + name)."""
        return self._get(f"/rest/api/3/project/{project_key.strip().upper()}/components")

    def resolve_component_names(self, project_key: str, names: list[str]) -> list[str]:
        """Match free-text component names against the project's actual components.

        Matching is case-insensitive and allows a substring match when exactly
        one component contains the given text.
        Raises ApiError if a name matches zero or more than one component.
        """
        available = self.list_components(project_key)
        resolved: list[str] = []
        for requested in names:
            folded = requested.casefold()
            exact = [c for c in available if c["name"].casefold() == folded]
            candidates = exact or [c for c in available if folded in c["name"].casefold()]
            if not candidates:
                known = ", ".join(c["name"] for c in available)
                raise ApiError(f"No component matching {requested!r} in {project_key}; known components: {known}")
            if len(candidates) > 1:
                names_found = ", ".join(c["name"] for c in candidates)
                raise ApiError(f"Ambiguous component {requested!r}, matches: {names_found}")
            resolved.append(candidates[0]["name"])
        return resolved

    def set_components(self, issue_key: str, component_names: list[str]) -> None:
        """Replace an issue's components with the given (already-resolved) names."""
        key = issue_key.strip().upper()
        self._write(
            "PUT",
            f"/rest/api/3/issue/{key}",
            {"fields": {"components": [{"name": name} for name in component_names]}},
        )
        self._issue_cache.pop(key, None)

    def find_account_id(self, query: str, project_key: str | None = None, issue_key: str | None = None) -> str:
        """Resolve a free-text query (name/email) to a single Jira accountId.

        Scoped to users assignable to `project_key`/`issue_key` when given, so we
        never resolve someone who couldn't actually be assigned to the task.
        """
        params: dict = {"query": query}
        if issue_key:
            params["issueKey"] = issue_key.strip().upper()
        elif project_key:
            params["project"] = project_key.strip().upper()
        path = "/rest/api/3/user/assignable/search" if (issue_key or project_key) else "/rest/api/3/user/search"
        matches = self._get(path, params=params)
        if not matches:
            raise ApiError(f"No assignable Jira user found matching {query!r}")
        if len(matches) > 1:
            names = ", ".join(f"{m.get('displayName')} ({m.get('accountId')})" for m in matches)
            raise ApiError(f"Ambiguous user {query!r}, matches: {names}")
        return matches[0]["accountId"]

    def list_assignable_users(
        self,
        project_key: str | None = None,
        issue_key: str | None = None,
        max_results: int = 50,
    ) -> list[dict]:
        """List Jira users assignable to `project_key`/`issue_key` (accountId + displayName).

        Paginates through all results rather than relying on the API's default
        50-result page, so callers see the full assignable set.
        """
        params: dict = {"maxResults": max_results}
        if issue_key:
            params["issueKey"] = issue_key.strip().upper()
        elif project_key:
            params["project"] = project_key.strip().upper()
        else:
            raise ValueError("Provide project_key or issue_key")

        users: list[dict] = []
        start_at = 0
        while True:
            params["startAt"] = start_at
            page = self._get("/rest/api/3/user/assignable/search", params=params)
            if not page:
                break
            users.extend(page)
            if len(page) < max_results:
                break
            start_at += max_results
        return users

    def is_assignable(self, account_id: str, project_key: str | None = None, issue_key: str | None = None) -> bool:
        """Check directly (not via a possibly-truncated listing) if account_id can be assigned."""
        params: dict = {"accountId": account_id}
        if issue_key:
            params["issueKey"] = issue_key.strip().upper()
        elif project_key:
            params["project"] = project_key.strip().upper()
        else:
            raise ValueError("Provide project_key or issue_key")
        matches = self._get("/rest/api/3/user/assignable/search", params=params)
        return any(u.get("accountId") == account_id for u in matches)

    def assign_issue(
        self,
        issue_key: str,
        account_id: str | None,
        verify: bool = True,
    ) -> None:
        """Assign (account_id set) or unassign (account_id None) an issue.

        When `verify` is set, the accountId is checked directly against Jira's
        assignable-users endpoint for this issue before the assignment PUT is sent.
        """
        key = issue_key.strip().upper()
        if account_id and verify and not self.is_assignable(account_id, issue_key=key):
            raise ApiError(f"Account {account_id!r} is not assignable to {key}")
        self._write("PUT", f"/rest/api/3/issue/{key}/assignee", {"accountId": account_id})

    def set_parent(self, issue_key: str, parent_key: str) -> None:
        """Set/change the parent issue (e.g. a subtask's story, or an epic link)."""
        key = issue_key.strip().upper()
        parent = parent_key.strip().upper()
        self.get_issue(parent)  # raises ApiError if the parent key does not exist
        self._write("PUT", f"/rest/api/3/issue/{key}", {"fields": {"parent": {"key": parent}}})
        self._issue_cache.pop(key, None)

    def list_transitions(self, issue_key: str) -> list[dict]:
        """Return the workflow transitions currently available for an issue."""
        key = issue_key.strip().upper()
        return self._get(f"/rest/api/3/issue/{key}/transitions").get("transitions", [])

    def transition_issue(self, issue_key: str, status_name: str) -> None:
        """Move an issue to `status_name` via its next matching workflow transition."""
        key = issue_key.strip().upper()
        transitions = self.list_transitions(key)
        match = next(
            (t for t in transitions if t["name"].casefold() == status_name.casefold()),
            None,
        )
        if match is None:
            available = ", ".join(t["name"] for t in transitions)
            raise ApiError(f"No transition to {status_name!r} for {key}; available: {available}")
        self._write("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
        self._issue_cache.pop(key, None)

    def get_active_sprint(self, board_id: int) -> dict | None:
        """Return the active sprint on `board_id`, or None if there isn't one."""
        result = self._get(f"/rest/agile/1.0/board/{board_id}/sprint", params={"state": "active"})
        sprints = result.get("values") or []
        return sprints[0] if sprints else None

    def add_issue_to_sprint(self, sprint_id: int, issue_key: str) -> None:
        """Move an existing issue into the given sprint."""
        self._write("POST", f"/rest/agile/1.0/sprint/{sprint_id}/issue", {"issues": [issue_key]})

    def update_issue(
        self,
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
    ) -> IssueInfo:
        """Update an issue's summary and/or plain-text description."""
        fields = {}
        if summary is not None:
            fields["summary"] = summary.strip()
        if description is not None:
            fields["description"] = self._description_document(description)
        if not fields:
            raise ValueError("At least one issue field must be provided")
        key = issue_key.strip().upper()
        self._write("PUT", f"/rest/api/3/issue/{key}", {"fields": fields})
        self._issue_cache.pop(key, None)
        return self.get_issue(key)

    @staticmethod
    def _description_document(description: str) -> dict:
        paragraphs = [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]}
            for line in description.splitlines()
        ]
        return {"type": "doc", "version": 1, "content": paragraphs}

    @staticmethod
    def _adf_to_text(value: object) -> str:
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return ""
        node_type = value.get("type")
        if node_type == "text":
            return str(value.get("text", ""))
        if node_type == "hardBreak":
            return "\n"
        text = "".join(JiraClient._adf_to_text(child) for child in value.get("content", []))
        return f"{text}\n" if node_type in {"paragraph", "heading", "listItem", "blockquote"} else text

    def list_assigned_issues(self) -> list[IssueInfo]:
        """List all issues assigned to the authenticated Jira user."""
        issues: list[IssueInfo] = []
        params = {
            "jql": "assignee = currentUser() ORDER BY updated DESC",
            "fields": "summary,status",
            "maxResults": 100,
        }
        while True:
            payload = self._get("/rest/api/3/search/jql", params=params)
            issues.extend(self._remember(item) for item in payload.get("issues", []))
            next_page_token = payload.get("nextPageToken")
            if payload.get("isLast", not next_page_token) or not next_page_token:
                return issues
            params["nextPageToken"] = next_page_token

    def issue_key_by_id(self, issue_id: int) -> str:
        """Tempo returns numeric issue ids only; map them back to human keys."""
        if issue_id not in self._key_by_id:
            info = self._remember(
                self._get(f"/rest/api/3/issue/{issue_id}", params={"fields": "summary,status"})
            )
            self._key_by_id[issue_id] = info.key
        return self._key_by_id[issue_id]

    def _remember(self, payload: dict) -> IssueInfo:
        fields = payload.get("fields", {})
        status = fields.get("status") or {}
        category = (status.get("statusCategory") or {}).get("key", "")
        info = IssueInfo(
            key=payload["key"],
            issue_id=int(payload["id"]),
            summary=fields.get("summary", ""),
            status=status.get("name", "unknown"),
            is_open=category != "done",
        )
        self._issue_cache[info.key] = info
        self._key_by_id[info.issue_id] = info.key
        return info


class TempoClient:
    """Worklog read/write against the Tempo v4 API."""

    def __init__(
        self,
        credentials: Credentials,
        jira: JiraClient,
        session: RetryingSession | None = None,
    ) -> None:
        self._credentials = credentials
        self._jira = jira
        self._session = session or RetryingSession()

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._credentials.tempo_token}", "Accept": "application/json"}
        response = self._session.request(
            method, f"{TEMPO_BASE_URL}{path}", params=params, json=body, headers=headers
        )
        if response.status_code >= 300:
            raise ApiError(f"Tempo API error ({response.status_code}) on {path}: {response.text[:300]}")
        return response.json() if response.content else {}

    def fetch_worklogs(self, from_date: date, to_date: date) -> list[Entry]:
        account_id = self._jira.account_id()
        page = self._paginate(
            f"/worklogs/user/{account_id}",
            {"from": from_date.isoformat(), "to": to_date.isoformat()},
        )
        return [self._to_entry(item, self._issue_key_of(item)) for item in page]

    def fetch_plans(self, from_date: date, to_date: date) -> list[PlannedWork]:
        """Tempo plans for the current user, expanded to one item per planned day."""
        account_id = self._jira.account_id()
        payload = self._call(
            "GET",
            f"/plans/user/{account_id}",
            params={
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "plannedTimeBreakdown": "DAILY",
            },
        )
        planned: list[PlannedWork] = []
        results = payload.get("results", [])
        log.debug("Tempo returned %d plan(s) for %s..%s", len(results), from_date, to_date)
        for item in results:
            plan_item = item.get("planItem") or {}
            if plan_item.get("type") != "ISSUE" or plan_item.get("id") is None:
                log.debug("Skipping plan %s of type %s", item.get("id"), plan_item.get("type"))
                continue
            try:
                issue_key = self._jira.issue_key_by_id(int(plan_item["id"]))
            except ApiError as exc:
                log.warning("Skipping plan for issue id %s: %s", plan_item["id"], exc)
                continue

            description = item.get("description") or ""
            start_minutes = parse_hhmm(item["startTime"]) if item.get("startTime") else None
            for day, minutes in self._planned_days(item, from_date, to_date):
                planned.append(
                    PlannedWork(
                        issue_key=issue_key,
                        description=description,
                        day=day,
                        minutes=minutes,
                        start_minutes=start_minutes,
                    )
                )
        return planned

    @staticmethod
    def _planned_days(item: dict, from_date: date, to_date: date) -> list[tuple[date, int]]:
        """Per-day planned minutes, from the DAILY breakdown or the plan's date range."""
        days = ((item.get("plannedTime") or {}).get("days") or {}).get("values") or []
        if days:
            return [
                (date.fromisoformat(value["date"]), int(value.get("plannedSeconds", 0)) // 60)
                for value in days
                if from_date <= date.fromisoformat(value["date"]) <= to_date
                and int(value.get("plannedSeconds", 0)) >= 60
            ]

        minutes = int(item.get("plannedSecondsPerDay") or item.get("secondsPerDay") or 0) // 60
        if minutes <= 0:
            return []
        include_weekends = bool(item.get("includeNonWorkingDays"))
        expanded: list[tuple[date, int]] = []
        day = max(date.fromisoformat(item["startDate"]), from_date)
        last = min(date.fromisoformat(item["endDate"]), to_date)
        while day <= last:
            if include_weekends or day.weekday() < 5:
                expanded.append((day, minutes))
            day += timedelta(days=1)
        return expanded

    def _paginate(self, path: str, params: dict) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            payload = self._call("GET", path, params={**params, "limit": PAGE_SIZE, "offset": offset})
            page = payload.get("results", [])
            items.extend(page)
            if len(page) < PAGE_SIZE:
                return items
            offset += len(page)

    def create_worklog(self, entry: Entry) -> int | None:
        issue = self._jira.get_issue(entry.issue)
        payload = {
            "issueId": issue.issue_id,
            "authorAccountId": self._jira.account_id(),
            "startDate": entry.day.isoformat(),
            "startTime": format_hhmmss(entry.interval.start),
            "timeSpentSeconds": entry.interval.duration * 60,
            "description": entry.description,
        }
        result = self._call("POST", "/worklogs", body=payload)
        return result.get("tempoWorklogId")

    @staticmethod
    def _to_entry(item: dict, issue_key: str) -> Entry:
        start = parse_hhmm(item.get("startTime") or "00:00:00")
        minutes = max(int(item.get("timeSpentSeconds", 0)) // 60, 1)
        return Entry(
            day=date.fromisoformat(item["startDate"]),
            issue=issue_key,
            description=item.get("description", ""),
            interval=Interval(start, start + minutes),
            tempo_worklog_id=item.get("tempoWorklogId"),
            synced=True,
        )

    def _issue_key_of(self, item: dict) -> str:
        issue = item.get("issue") or {}
        if issue.get("key"):
            return issue["key"]
        issue_id = issue.get("id")
        if issue_id is None:
            return "?"
        try:
            return self._jira.issue_key_by_id(int(issue_id))
        except ApiError as exc:
            log.warning("Could not resolve issue id %s: %s", issue_id, exc)
            return str(issue_id)
