"""get_user_activity: what one user did in Jira (and Confluence) in a date range.

Partial results beat errors: once the issue search succeeds, a failure on one
issue (or on Confluence) becomes a note in the output, never a failed call.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .adf import adf_to_markdown
from .client import MYSELF_PATH, AtlassianError, ConfluenceClient, JiraClient
from .config import ConfigError

logger = logging.getLogger(__name__)

MAX_CONCURRENT_ISSUES = 5
MAX_ISSUES_LIMIT = 100
PAGE_SIZE = 100
# Bounds the cost of issues with enormous histories (100 entries per page).
MAX_CHANGELOG_PAGES = 20
MAX_CONFLUENCE_RESULTS = 50
EDIT_DETAIL_CHARS = 80
COMMENT_DETAIL_CHARS = 200
SEARCH_FIELDS = ["summary", "status", "created", "creator"]

_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9:_-]+$")

# The authenticated account (/myself), cached for the process lifetime.
_me: dict[str, Any] | None = None


@dataclass(frozen=True)
class Event:
    time: datetime
    key: str
    kind: str
    detail: str


@dataclass
class IssueActivity:
    key: str
    summary: str
    status: str
    events: list[Event] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    tz: tzinfo

    def __contains__(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


async def _get_me(client: JiraClient) -> dict[str, Any]:
    global _me
    if _me is None:
        _me = dict(await client.get_json(MYSELF_PATH))
    return _me


def _parse_day(raw: str, name: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(
            f"{name}='{raw}' is not a date in yyyy-MM-dd format (e.g. 2026-10-30)."
        ) from None


def _timezone(name: Any) -> tzinfo:
    try:
        return ZoneInfo(str(name)) if name else ZoneInfo("UTC")
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown time zone %r; using UTC", name)
        return ZoneInfo("UTC")


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=ZoneInfo("UTC"))


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 1]}…"


def _account_id(obj: Any) -> str:
    return str(obj.get("accountId") or "") if isinstance(obj, dict) else ""


def _mentions(node: Any) -> list[str]:
    if isinstance(node, list):
        return [name for child in node for name in _mentions(child)]
    if not isinstance(node, dict):
        return []
    if node.get("type") == "mention":
        text = str((node.get("attrs") or {}).get("text") or "@unknown")
        return [text if text.startswith("@") else f"@{text}"]
    return _mentions(node.get("content"))


def _changelog_event(key: str, moment: datetime, item: dict[str, Any]) -> Event:
    name = str(item.get("field") or item.get("fieldId") or "?")
    old = str(item.get("fromString") or item.get("from") or "")
    new = str(item.get("toString") or item.get("to") or "")
    if name.lower() == "status":
        return Event(moment, key, "transition", f"{old or '?'} -> {new or '?'}")
    change = f"{old or '(empty)'} -> {new or '(empty)'}"
    return Event(moment, key, "edit", f"{name}: {_shorten(change, EDIT_DETAIL_CHARS)}")


async def _changelog_events(
    client: JiraClient, key: str, account_id: str, window: Window
) -> list[Event]:
    events: list[Event] = []
    start_at = 0
    for _ in range(MAX_CHANGELOG_PAGES):
        page = await client.get_json(
            f"/rest/api/3/issue/{key}/changelog",
            params={"startAt": start_at, "maxResults": PAGE_SIZE},
        )
        histories = [h for h in page.get("values") or [] if isinstance(h, dict)]
        for history in histories:
            moment = _parse_time(history.get("created"))
            if _account_id(history.get("author")) != account_id:
                continue
            if moment is None or moment not in window:
                continue
            events.extend(
                _changelog_event(key, moment, item)
                for item in history.get("items") or []
                if isinstance(item, dict)
            )
        start_at += len(histories)
        total = page.get("total")
        if page.get("isLast", True) or not histories:
            break
        if isinstance(total, int) and start_at >= total:
            break
    return events


def _comment_detail(comment: dict[str, Any]) -> str:
    body = comment.get("body")
    detail = _shorten(adf_to_markdown(body), COMMENT_DETAIL_CHARS)
    mentioned = sorted(set(_mentions(body)))
    if mentioned:
        detail += f" (mentions {', '.join(mentioned)})"
    return detail or "*(empty comment)*"


async def _comment_events(
    client: JiraClient, key: str, account_id: str, window: Window
) -> list[Event]:
    """Newest first, so paging stops at the first comment older than the window."""
    events: list[Event] = []
    start_at = 0
    while True:
        page = await client.get_json(
            f"/rest/api/3/issue/{key}/comment",
            params={
                "startAt": start_at,
                "maxResults": PAGE_SIZE,
                "orderBy": "-created",
            },
        )
        comments = [c for c in page.get("comments") or [] if isinstance(c, dict)]
        for comment in comments:
            moment = _parse_time(comment.get("created"))
            if moment is None:
                continue
            if moment < window.start:
                return events
            if moment in window and _account_id(comment.get("author")) == account_id:
                events.append(Event(moment, key, "comment", _comment_detail(comment)))
        start_at += len(comments)
        total = page.get("total")
        if not comments or (isinstance(total, int) and start_at >= total):
            return events


async def _issue_activity(
    client: JiraClient, issue: dict[str, Any], account_id: str, window: Window
) -> IssueActivity:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "?")
    activity = IssueActivity(
        key=key,
        summary=str(fields.get("summary") or ""),
        status=str((fields.get("status") or {}).get("name") or "?"),
    )
    created = _parse_time(fields.get("created"))
    if (
        created is not None
        and created in window
        and _account_id(fields.get("creator")) == account_id
    ):
        activity.events.append(Event(created, key, "create", activity.summary))
    try:
        changelog, comments = await asyncio.gather(
            _changelog_events(client, key, account_id, window),
            _comment_events(client, key, account_id, window),
        )
    except AtlassianError as exc:
        activity.error = str(exc)
        return activity
    activity.events.extend(changelog)
    activity.events.extend(comments)
    activity.events.sort(key=lambda event: event.time)
    return activity


async def _confluence_events(
    client: ConfluenceClient, account_id: str, from_day: date, to_day: date
) -> tuple[list[Event], str | None]:
    cql = (
        f'contributor = "{account_id}" AND lastmodified >= "{from_day}" '
        f'AND lastmodified < "{to_day}" ORDER BY lastmodified DESC'
    )
    data = await client.get_json(
        "/wiki/rest/api/search",
        params={"cql": cql, "limit": MAX_CONFLUENCE_RESULTS},
    )
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    events: list[Event] = []
    for result in results:
        moment = _parse_time(result.get("lastModified"))
        if moment is None:
            continue
        content = result.get("content") or {}
        title = html.unescape(str(content.get("title") or result.get("title") or "?"))
        title = re.sub(r"@@@(?:end)?hl@@@", "", title)
        space = str((result.get("resultGlobalContainer") or {}).get("title") or "?")
        path = result.get("url") or (content.get("_links") or {}).get("webui")
        url = f"{client.site_url}/wiki{path}" if path else ""
        detail = f"{title} [{space}]" + (f" <{url}>" if url else "")
        events.append(Event(moment, "Confluence", "page_edit", detail))
    total = data.get("totalSize")
    note = None
    if isinstance(total, int) and total > len(results):
        note = f"Confluence: showing {len(results)} of {total} matching pages."
    return events, note


async def _user_name(client: JiraClient, account_id: str, me: dict[str, Any]) -> str:
    if account_id == me.get("accountId"):
        return str(me.get("displayName") or account_id)
    try:
        user = await client.get_json(
            "/rest/api/3/user", params={"accountId": account_id}
        )
    except AtlassianError as exc:
        logger.warning("Could not resolve Jira user %s: %s", account_id, exc)
        return account_id
    return str(user.get("displayName") or account_id)


async def get_user_activity_impl(
    client: JiraClient,
    confluence_client: Callable[[], ConfluenceClient] | None,
    from_date: str,
    to_date: str,
    user: str = "me",
    max_issues: int = 50,
) -> str:
    """`confluence_client` is a factory so Confluence config is only touched
    when requested; None skips Confluence."""
    from_day = _parse_day(from_date, "from_date")
    to_day = _parse_day(to_date, "to_date")
    if to_day <= from_day:
        raise ValueError(
            "to_date is exclusive and must be after from_date; for a single day "
            f"pass to_date={from_day + timedelta(days=1)}."
        )
    user = user.strip() or "me"
    me = await _get_me(client)
    if user.lower() == "me":
        account_id = str(me.get("accountId") or "")
    elif _ACCOUNT_ID_RE.match(user):
        account_id = user
    else:
        raise ValueError(
            f"user='{user}' must be \"me\" or an Atlassian accountId "
            "(e.g. 5b10ac8d82e05b22cc7d4ef5), not a name or email."
        )
    if not account_id:
        raise AtlassianError("Could not determine the authenticated accountId.")

    tz = _timezone(me.get("timeZone"))
    window = Window(
        start=datetime.combine(from_day, datetime.min.time(), tz),
        end=datetime.combine(to_day, datetime.min.time(), tz),
        tz=tz,
    )
    limit = max(1, min(max_issues, MAX_ISSUES_LIMIT))
    result = await client.search_jql(
        {
            "jql": (
                f'issue in updatedBy("{account_id}", "{from_day}", "{to_day}") '
                "ORDER BY updated DESC"
            ),
            "maxResults": limit,
            "fields": SEARCH_FIELDS,
        }
    )
    issues = [i for i in result.get("issues") or [] if isinstance(i, dict)][:limit]
    truncated = bool(result.get("nextPageToken"))

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_ISSUES)

    async def bounded(awaitable: Awaitable[IssueActivity]) -> IssueActivity:
        async with semaphore:
            return await awaitable

    activities, name = await asyncio.gather(
        asyncio.gather(
            *(bounded(_issue_activity(client, i, account_id, window)) for i in issues)
        ),
        _user_name(client, account_id, me),
    )

    page_events: list[Event] = []
    notes: list[str] = []
    if truncated:
        notes.append(
            f"More issues matched than max_issues={limit}; only the {limit} most "
            "recently updated are shown. Narrow the dates or raise max_issues "
            f"(max {MAX_ISSUES_LIMIT})."
        )
    notes.extend(
        f"{a.key} failed to load, its events are missing: {a.error}"
        for a in activities
        if a.error
    )
    if confluence_client is not None:
        try:
            page_events, note = await _confluence_events(
                confluence_client(), account_id, from_day, to_day
            )
        except (AtlassianError, ConfigError) as exc:
            notes.append(f"Confluence activity unavailable: {exc}")
        else:
            if note:
                notes.append(note)

    return render_activity(
        name, account_id, from_day, to_day, window, list(activities), page_events, notes
    )


def _stamp(moment: datetime, window: Window, multi_day: bool) -> str:
    local = moment.astimezone(window.tz)
    return local.strftime("%m-%d %H:%M" if multi_day else "%H:%M")


def render_activity(
    name: str,
    account_id: str,
    from_day: date,
    to_day: date,
    window: Window,
    activities: list[IssueActivity],
    page_events: list[Event],
    notes: list[str],
) -> str:
    multi_day = to_day - from_day > timedelta(days=1)
    with_events = [a for a in activities if a.events]
    timeline = sorted(
        [e for a in activities for e in a.events] + page_events,
        key=lambda event: event.time,
    )
    lines = [
        f"# Activity of {name} ({account_id}), {from_day} to {to_day} (exclusive)",
        "",
        (
            f"Times in {window.tz}. {len(with_events)} issues with events, "
            f"{len(timeline)} events."
        ),
    ]
    if notes:
        lines.append("")
        lines.extend(f"> **Note:** {note}" for note in notes)

    for activity in with_events:
        lines.extend(
            ["", f"## {activity.key}: {activity.summary} [{activity.status}]", ""]
        )
        lines.extend(
            f"- {_stamp(e.time, window, multi_day)}  {e.kind}  {e.detail}"
            for e in activity.events
        )

    quiet = [a.key for a in activities if not a.events and not a.error]
    if quiet:
        lines.extend(
            [
                "",
                (
                    "*(updated by this user, but no comment, transition or field edit "
                    f"found, e.g. worklogs or attachments only: {', '.join(quiet)})*"
                ),
            ]
        )

    if page_events:
        lines.extend(["", "## Confluence", ""])
        lines.extend(
            f"- {_stamp(e.time, window, multi_day)}  {e.kind}  {e.detail}"
            for e in sorted(page_events, key=lambda event: event.time)
        )

    lines.extend(["", "## Timeline", ""])
    if not timeline:
        lines.append("*(no activity found)*")
    lines.extend(
        f"- {_stamp(e.time, window, multi_day)}  {e.key}  {e.kind}  {e.detail}"
        for e in timeline
    )
    return "\n".join(lines)
