"""Tests for the opt-in write client and write tools (mocked HTTP)."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from jira_mcp import writes
from jira_mcp.client import (
    JiraClient,
    JiraWriteClient,
    ReadOnlyViolationError,
    WriteNotAllowedError,
)
from jira_mcp.config import Settings, _parse_projects

SITE = "https://example.atlassian.net"
ACCOUNT = {"accountId": "abc", "displayName": "Ada"}

FIELDS = [
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10101", "name": "Acceptance Criteria"},
    {"id": "customfield_10014", "name": "Epic Link"},
    {"id": "customfield_1", "name": "Dup"},
    {"id": "customfield_2", "name": "Dup"},
]
LINK_TYPES = {
    "issueLinkTypes": [
        {"id": "1", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
        {"id": "2", "name": "Relates", "inward": "relates to", "outward": "relates to"},
    ]
}


def settings(projects: str = "AC", token: str | None = "write-secret") -> Settings:
    return Settings(
        email="ada@x.io",
        api_token="read-secret",
        site_url=SITE,
        cloud_id=None,
        env_file_found=True,
        jira_write_api_token=token,
        jira_write_projects=_parse_projects(projects),
    )


class Recorder:
    """Serves the canned reads and records every non-GET request."""

    def __init__(
        self, extra: Callable[[httpx.Request], httpx.Response | None] | None = None
    ):
        self.writes: list[tuple[str, str, Any]] = []
        self.extra = extra

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.extra and (response := self.extra(request)) is not None:
            return response
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if path == "/rest/api/3/field":
            return httpx.Response(200, json=FIELDS)
        if path == "/rest/api/2/issueLinkType":
            return httpx.Response(200, json=LINK_TYPES)
        if request.method != "GET":
            body = json.loads(request.content) if request.content else None
            self.writes.append((request.method, path, body))
            if path == "/rest/api/2/issue":
                return httpx.Response(201, json={"key": "AC-99"})
            if path.endswith("/comment"):
                return httpx.Response(201, json={"id": "555"})
            return httpx.Response(204)
        return httpx.Response(404)


def make(handler: Recorder, projects: str = "AC") -> JiraWriteClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraWriteClient(settings(projects), http=http)


def test_parse_projects() -> None:
    assert _parse_projects(" ac, Plat ,,") == frozenset({"AC", "PLAT"})


def test_writes_need_projects_token_falls_back() -> None:
    assert settings().writes_enabled
    assert not settings(projects="").writes_enabled
    assert settings(token=None).writes_enabled
    assert settings(token=None).write_token == "read-secret"
    assert settings().write_token == "write-secret"
    with pytest.raises(WriteNotAllowedError):
        JiraWriteClient(settings(projects=""))


async def test_read_client_still_blocks_writes() -> None:
    client = JiraClient(settings())
    with pytest.raises(ReadOnlyViolationError):
        await client._request("PUT", "/rest/api/2/issue/AC-1", json_body={})


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/rest/api/2/issue/AC-1"),
        ("DELETE", "/rest/api/2/issue/AC-1/comment/5"),
        ("PUT", "/rest/api/3/issue/AC-1"),
        ("POST", "/rest/api/2/issue/AC-1/attachments"),
        ("PUT", "/rest/api/2/issue/OTHER-1"),
    ],
)
async def test_write_client_blocks_other_routes(method: str, path: str) -> None:
    recorder = Recorder()
    with pytest.raises(WriteNotAllowedError):
        await make(recorder).send(method, path, {})
    assert recorder.writes == []


async def test_create_and_link_check_projects_in_body() -> None:
    recorder = Recorder()
    client = make(recorder)
    with pytest.raises(WriteNotAllowedError):
        await writes.create_issue_impl(client, "OTHER", "x")
    with pytest.raises(WriteNotAllowedError):
        await client.send(
            "POST",
            "/rest/api/2/issueLink",
            {
                "type": {"name": "Blocks"},
                "inwardIssue": {"key": "AC-1"},
                "outwardIssue": {"key": "OTHER-2"},
            },
        )
    assert recorder.writes == []


async def test_update_issue_resolves_field_names() -> None:
    recorder = Recorder()
    result = await writes.update_issue_impl(
        make(recorder),
        "ac-69",
        description="h3. New",
        fields={"Acceptance Criteria": "* one", "customfield_10014": "AC-38"},
    )
    assert recorder.writes == [
        (
            "PUT",
            "/rest/api/2/issue/AC-69",
            {
                "fields": {
                    "customfield_10101": "* one",
                    "customfield_10014": "AC-38",
                    "description": "h3. New",
                }
            },
        )
    ]
    assert result.startswith("Updated AC-69:")


async def test_unknown_or_ambiguous_field_is_rejected() -> None:
    recorder = Recorder()
    with pytest.raises(ValueError, match="doesn't exist"):
        await writes.update_issue_impl(make(recorder), "AC-1", fields={"Nope": 1})
    with pytest.raises(ValueError, match="ambiguous"):
        await writes.update_issue_impl(make(recorder), "AC-1", fields={"Dup": 1})
    assert recorder.writes == []


def users(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/rest/api/3/user/assignable/search":
        return httpx.Response(
            200,
            json=[
                {"accountId": "m1", "displayName": "Mathias Test"},
                {"accountId": "m2", "displayName": "Mathias Test Two"},
            ],
        )
    return None


async def test_assignee_exact_match_and_unassign() -> None:
    recorder = Recorder(users)
    client = make(recorder)
    await writes.update_issue_impl(client, "AC-1", assignee="mathias test")
    await writes.update_issue_impl(client, "AC-1", assignee="none")
    assert recorder.writes[0][2] == {"fields": {"assignee": {"accountId": "m1"}}}
    assert recorder.writes[1][2] == {"fields": {"assignee": None}}


async def test_assignee_ambiguous() -> None:
    with pytest.raises(ValueError, match="matches 2"):
        await writes.update_issue_impl(make(Recorder(users)), "AC-1", assignee="Math")


async def test_create_issue_with_parent() -> None:
    recorder = Recorder()
    result = await writes.create_issue_impl(
        make(recorder), "ac", "Keychain in PROD", "text", parent="AC-38"
    )
    assert recorder.writes[0][2]["fields"]["project"] == {"key": "AC"}
    assert recorder.writes[0][2]["fields"]["parent"] == {"key": "AC-38"}
    assert result.startswith("Created AC-99")


def transitions(request: httpx.Request) -> httpx.Response | None:
    if request.method == "GET" and request.url.path.endswith("/transitions"):
        return httpx.Response(
            200,
            json={
                "transitions": [{"id": "31", "name": "Done", "to": {"name": "DONE"}}]
            },
        )
    return None


async def test_transition_by_status_name() -> None:
    recorder = Recorder(transitions)
    assert "DONE" in await writes.transition_issue_impl(make(recorder), "AC-30", "done")
    assert recorder.writes == [
        ("POST", "/rest/api/2/issue/AC-30/transitions", {"transition": {"id": "31"}})
    ]
    with pytest.raises(ValueError, match="Possible: DONE"):
        await writes.transition_issue_impl(make(recorder), "AC-30", "Review")


def linked(request: httpx.Request) -> httpx.Response | None:
    if request.method == "GET" and request.url.path == "/rest/api/3/issue/AC-66":
        return httpx.Response(
            200,
            json={
                "fields": {
                    "issuelinks": [
                        {
                            "id": "700",
                            "type": LINK_TYPES["issueLinkTypes"][0],
                            "inwardIssue": {"key": "AC-14"},
                        }
                    ]
                }
            },
        )
    return None


async def test_link_is_blocked_by() -> None:
    recorder = Recorder(linked)
    result = await writes.link_issues_impl(
        make(recorder), "AC-66", "is blocked by", "AC-14"
    )
    assert recorder.writes == [
        (
            "POST",
            "/rest/api/2/issueLink",
            {
                "type": {"name": "Blocks"},
                "inwardIssue": {"key": "AC-14"},
                "outwardIssue": {"key": "AC-66"},
            },
        )
    ]
    assert result == "Linked: AC-66 is blocked by AC-14 (link 700)."


async def test_link_direction_mismatch_is_reported() -> None:
    with pytest.raises(Exception, match="link 700"):
        await writes.link_issues_impl(
            make(Recorder(linked)), "AC-66", "blocks", "AC-14"
        )


async def test_unlink_checks_both_projects() -> None:
    def link(request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET" and request.url.path == "/rest/api/2/issueLink/700":
            return httpx.Response(
                200,
                json={
                    "type": LINK_TYPES["issueLinkTypes"][0],
                    "inwardIssue": {"key": "AC-14"},
                    "outwardIssue": {"key": "OTHER-1"},
                },
            )
        return None

    recorder = Recorder(link)
    with pytest.raises(WriteNotAllowedError):
        await writes.unlink_issues_impl(make(recorder), 700)
    assert recorder.writes == []


async def test_comment_add_and_edit() -> None:
    recorder = Recorder()
    client = make(recorder)
    assert "comment 555" in await writes.add_comment_impl(client, "AC-74", "hi")
    await writes.edit_comment_impl(client, "AC-45", "123", "synthetic")
    assert recorder.writes == [
        ("POST", "/rest/api/2/issue/AC-74/comment", {"body": "hi"}),
        ("PUT", "/rest/api/2/issue/AC-45/comment/123", {"body": "synthetic"}),
    ]


async def test_register_adds_tools_and_instructions() -> None:
    mcp = FastMCP(
        name="t",
        instructions="Constraints: strictly read-only; nothing can be created, "
        "edited, commented on, or transitioned.",
    )
    client = make(Recorder())
    writes.register(mcp, lambda: client)
    names = {tool.name for tool in await mcp.list_tools()}
    assert {"update_issue", "create_issue", "link_issues", "unlink_issues"} <= names
    assert "enabled for Jira projects AC only" in (mcp.instructions or "")
