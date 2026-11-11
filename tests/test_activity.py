"""Tests for get_user_activity (mocked HTTP)."""

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from jira_mcp import activity, server
from jira_mcp.client import ConfluenceClient, JiraClient
from jira_mcp.config import ConfigError, Settings, Site, parse_sites

SITE = "https://example.atlassian.net"
ME = {"accountId": "me-1", "displayName": "Ada", "timeZone": "Europe/Berlin"}
OTHER = "other-2"


@pytest.fixture(autouse=True)
def reset_me_cache() -> Iterator[None]:
    activity._me = None
    yield
    activity._me = None


def settings() -> Settings:
    return Settings(
        email="ada@x.io",
        api_token="secret",
        sites=parse_sites(SITE),
        cloud_id=None,
        env_file_found=True,
    )


def issue(key: str, created: str = "2026-01-01T10:00:00.000+0100") -> dict[str, Any]:
    return {
        "key": key,
        "fields": {
            "summary": f"Summary {key}",
            "status": {"name": "In Progress"},
            "created": created,
            "creator": {"accountId": "me-1"},
        },
    }


def history(author: str, created: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"author": {"accountId": author}, "created": created, "items": list(items)}


def comment(author: str, created: str, text: str) -> dict[str, Any]:
    return {
        "author": {"accountId": author},
        "created": created,
        "body": {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": text + " "},
                        {"type": "mention", "attrs": {"text": "@Bob"}},
                    ],
                }
            ],
        },
    }


class Handler:
    """Serves /myself, the JQL search, and per-issue changelog/comment pages."""

    def __init__(
        self,
        issues: list[dict[str, Any]],
        changelogs: dict[str, list[dict[str, Any]]] | None = None,
        comment_pages: dict[str, list[list[dict[str, Any]]]] | None = None,
        next_page_token: str | None = None,
        failing: frozenset[str] = frozenset(),
    ) -> None:
        self.issues = issues
        self.changelogs = changelogs or {}
        self.comment_pages = comment_pages or {}
        self.next_page_token = next_page_token
        self.failing = failing
        self.myself_requests = 0
        self.comment_requests: list[int] = []
        self.jql: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == "/rest/api/3/myself":
            self.myself_requests += 1
            return httpx.Response(200, json=ME)
        if path == "/rest/api/3/search/jql":
            body = json.loads(request.content)
            self.jql.append(body["jql"])
            payload: dict[str, Any] = {"issues": self.issues}
            if self.next_page_token:
                payload["nextPageToken"] = self.next_page_token
            return httpx.Response(200, json=payload)
        if path == "/rest/api/3/user":
            return httpx.Response(200, json={"displayName": "Other Person"})
        parts = path.split("/")
        if len(parts) == 7 and parts[4] == "issue":
            key, resource = parts[5], parts[6]
            if key in self.failing:
                return httpx.Response(500, text="boom")
            if resource == "changelog":
                values = self.changelogs.get(key, [])
                start = int(params["startAt"])
                chunk = values[start : start + 2]
                return httpx.Response(
                    200,
                    json={
                        "values": chunk,
                        "startAt": start,
                        "total": len(values),
                        "isLast": start + len(chunk) >= len(values),
                    },
                )
            if resource == "comment":
                assert params["orderBy"] == "-created"
                pages = self.comment_pages.get(key, [])
                start = int(params["startAt"])
                self.comment_requests.append(start)
                index = start // 2
                chunk = pages[index] if index < len(pages) else []
                return httpx.Response(
                    200,
                    json={
                        "comments": chunk,
                        "startAt": start,
                        "total": sum(len(p) for p in pages),
                    },
                )
        return httpx.Response(404)


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> JiraClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(settings(), http=http)


async def run(handler: Handler, **kwargs: Any) -> str:
    client = make_client(handler)
    args: dict[str, Any] = {"from_date": "2026-10-30", "to_date": "2026-10-31"}
    args.update(kwargs)
    return await activity.get_user_activity_impl(client, None, **args)


async def test_me_resolves_via_myself_and_is_cached() -> None:
    handler = Handler(issues=[])
    client = make_client(handler)
    await activity.get_user_activity_impl(client, None, "2026-10-30", "2026-10-31")
    await activity.get_user_activity_impl(client, None, "2026-10-30", "2026-10-31")
    assert handler.jql[0] == (
        'issue in updatedBy("me-1", "2026-10-30", "2026-10-31") ORDER BY updated DESC'
    )
    # One probe during base-URL detection plus one cached /myself lookup.
    assert handler.myself_requests == 2


async def test_explicit_account_id_is_used_literally() -> None:
    handler = Handler(issues=[])
    output = await run(handler, user=OTHER)
    assert 'updatedBy("other-2"' in handler.jql[0]
    assert "# Activity of Other Person (other-2)" in output


async def test_invalid_user_is_rejected() -> None:
    with pytest.raises(ValueError, match="accountId"):
        await run(Handler(issues=[]), user='x") OR project = "Y')


async def test_to_date_must_follow_from_date() -> None:
    with pytest.raises(ValueError, match="2026-10-31"):
        await run(Handler(issues=[]), to_date="2026-10-30")


async def test_changelog_and_comments_filtered_by_author_and_window() -> None:
    changelog = [
        history(
            "me-1",
            "2026-10-29T23:30:00.000+0100",  # before window (Berlin)
            {"field": "status", "fromString": "Open", "toString": "To Do"},
        ),
        history(
            "me-1",
            "2026-10-30T08:15:00.000+0000",  # 09:15 Berlin
            {"field": "status", "fromString": "To Do", "toString": "In Progress"},
            {"field": "labels", "fromString": "", "toString": "x" * 200},
        ),
        history(
            OTHER,
            "2026-10-30T10:00:00.000+0100",
            {"field": "status", "fromString": "In Progress", "toString": "Done"},
        ),
    ]
    comments = [
        [
            comment("me-1", "2026-10-30T16:00:00.000+0100", "Shipped it"),
            comment(OTHER, "2026-10-30T15:00:00.000+0100", "Not mine"),
        ]
    ]
    handler = Handler(
        issues=[issue("PROJ-1", created="2026-10-30T08:00:00.000+0100")],
        changelogs={"PROJ-1": changelog},
        comment_pages={"PROJ-1": comments},
    )
    output = await run(handler)

    assert "## PROJ-1: Summary PROJ-1 [In Progress]" in output
    assert "- 08:00  create  Summary PROJ-1" in output
    assert "- 09:15  transition  To Do -> In Progress" in output
    assert "Open -> To Do" not in output
    assert "Done" not in output
    assert "- 16:00  comment  Shipped it @Bob (mentions @Bob)" in output
    assert "Not mine" not in output
    edit_line = next(line for line in output.splitlines() if "edit  labels" in line)
    assert edit_line.endswith("…")
    assert len(edit_line.split("labels: ", 1)[1]) == 80

    timeline = output.split("## Timeline", 1)[1].strip().splitlines()
    assert [line.split("  ")[2] for line in timeline] == [
        "create",
        "transition",
        "edit",
        "comment",
    ]
    assert timeline[0].startswith("- 08:00  PROJ-1  create")


async def test_comment_pagination_stops_at_first_comment_before_window() -> None:
    pages = [
        [
            comment("me-1", "2026-10-30T12:00:00.000+0100", "Newest"),
            comment("me-1", "2026-10-30T11:00:00.000+0100", "Middle"),
        ],
        [
            comment("me-1", "2026-10-30T10:00:00.000+0100", "Oldest in range"),
            comment("me-1", "2026-10-29T10:00:00.000+0100", "Too old"),
        ],
        [comment("me-1", "2026-10-28T10:00:00.000+0100", "Never fetched")],
    ]
    handler = Handler(issues=[issue("PROJ-1")], comment_pages={"PROJ-1": pages})
    output = await run(handler)
    assert handler.comment_requests == [0, 2]
    assert "Oldest in range" in output
    assert "Too old" not in output


async def test_changelog_is_paginated() -> None:
    changelog = [
        history(
            "me-1",
            f"2026-10-30T1{i}:00:00.000+0100",
            {"field": "priority", "fromString": "Low", "toString": f"P{i}"},
        )
        for i in range(5)
    ]
    handler = Handler(issues=[issue("PROJ-1")], changelogs={"PROJ-1": changelog})
    output = await run(handler)
    assert all(f"priority: Low -> P{i}" in output for i in range(5))


async def test_truncation_notice_when_more_issues_match() -> None:
    handler = Handler(issues=[issue("PROJ-1"), issue("PROJ-2")], next_page_token="t")
    output = await run(handler, max_issues=2)
    assert "> **Note:** More issues matched than max_issues=2" in output


async def test_one_failing_issue_does_not_fail_the_call() -> None:
    changelog = [
        history(
            "me-1",
            "2026-10-30T09:00:00.000+0100",
            {"field": "status", "fromString": "To Do", "toString": "Done"},
        )
    ]
    handler = Handler(
        issues=[issue("PROJ-1"), issue("PROJ-2")],
        changelogs={"PROJ-1": changelog},
        failing=frozenset({"PROJ-2"}),
    )
    output = await run(handler)
    assert "- 09:00  transition  To Do -> Done" in output
    assert "> **Note:** PROJ-2 failed to load" in output


async def test_issue_without_extracted_events_is_listed_as_quiet() -> None:
    output = await run(Handler(issues=[issue("PROJ-9")]))
    assert "attachments only: PROJ-9" in output
    assert "*(no activity found)*" in output


async def test_confluence_off_never_touches_confluence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(site: Site) -> ConfluenceClient:
        raise AssertionError("Confluence must not be touched")

    client = make_client(Handler(issues=[]))
    monkeypatch.setattr(server, "_settings", settings())
    monkeypatch.setattr(server, "_get_client", lambda site: client)
    monkeypatch.setattr(server, "_get_confluence_client", explode)
    output = await server.get_user_activity("2026-10-30", "2026-10-31")
    assert "## Confluence" not in output
    assert "Confluence activity unavailable" not in output


def confluence_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/wiki/rest/api/user/current":
        return httpx.Response(200, json={"accountId": "me-1"})
    if path == "/wiki/rest/api/search":
        assert request.url.params["cql"].startswith(
            'contributor = "me-1" AND type in (page, blogpost) '
            'AND lastmodified >= "2026-10-30" '
            'AND lastmodified < "2026-10-31"'
        )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "content": {"title": "Release notes"},
                        "resultGlobalContainer": {"title": "Engineering"},
                        "url": "/spaces/ENG/pages/42/Release+notes",
                        "lastModified": "2026-10-30T13:30:00.000Z",
                    }
                ],
                "totalSize": 1,
            },
        )
    return httpx.Response(404)


async def test_confluence_on_lists_page_edits() -> None:
    wiki = ConfluenceClient(
        settings(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(confluence_handler)),
    )
    output = await activity.get_user_activity_impl(
        make_client(Handler(issues=[])), lambda: wiki, "2026-10-30", "2026-10-31"
    )
    expected = (
        f"14:30  page_edit  Release notes [Engineering] "
        f"<{SITE}/wiki/spaces/ENG/pages/42/Release+notes>"
    )
    assert f"## Confluence\n\n- {expected}" in output
    assert "- 14:30  Confluence  page_edit  Release notes" in output


async def test_confluence_failure_becomes_a_note() -> None:
    def broken() -> ConfluenceClient:
        raise ConfigError("CONFLUENCE_API_TOKEN missing")

    output = await activity.get_user_activity_impl(
        make_client(Handler(issues=[])), broken, "2026-10-30", "2026-10-31"
    )
    assert "> **Note:** Confluence activity unavailable" in output
