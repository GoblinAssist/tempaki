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
