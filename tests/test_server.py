"""Tests for the field-name catalog: caching, wiring, graceful degradation."""

from collections.abc import Callable, Iterator

import httpx
import pytest

from jira_mcp import server
from jira_mcp.client import JiraClient
from jira_mcp.config import Settings, parse_sites
from jira_mcp.server import _get_issue_context_impl, _get_issue_impl

SITE = "https://example.atlassian.net"
ACCOUNT = {"accountId": "abc", "displayName": "Ada"}

FIELD_CATALOG = [
    {"id": "summary", "name": "Summary"},
    {"id": "customfield_10101", "name": "Acceptance Criteria"},
]

ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "Add tags to cards",
        "customfield_10101": "Tags must be colored per Contentful config",
    },
}


@pytest.fixture(autouse=True)
def reset_field_name_cache() -> Iterator[None]:
    server._field_names.clear()
    yield
    server._field_names.clear()


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> JiraClient:
    settings = Settings(
        email="ada@x.io",
        api_token="secret",
        sites=parse_sites(SITE),
        cloud_id=None,
        env_file_found=True,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return JiraClient(settings, http=http)


class Handler:
    def __init__(self, catalog_status: int = 200) -> None:
        self.catalog_status = catalog_status
        self.catalog_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SITE}/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if url == f"{SITE}/rest/api/3/field":
            self.catalog_requests += 1
            if self.catalog_status != 200:
                return httpx.Response(self.catalog_status)
            return httpx.Response(200, json=FIELD_CATALOG)
        if url.startswith(f"{SITE}/rest/api/3/issue/PROJ-1"):
            assert request.url.params["fields"] == "*all,-comment,-worklog"
            return httpx.Response(200, json=ISSUE)
        return httpx.Response(404)


async def test_custom_fields_appear_with_display_names() -> None:
    client = make_client(Handler())
    output = await _get_issue_impl(client, "PROJ-1", include_comments=False)
    assert "## Custom fields" in output
    assert (
        "- **Acceptance Criteria:** Tags must be colored per Contentful config"
        in output
    )


async def test_field_catalog_is_fetched_once() -> None:
    handler = Handler()
    client = make_client(handler)
    await _get_issue_impl(client, "PROJ-1", include_comments=False)
    await _get_issue_impl(client, "PROJ-1", include_comments=False)
    assert handler.catalog_requests == 1


async def test_catalog_failure_degrades_to_raw_ids() -> None:
    handler = Handler(catalog_status=500)
    client = make_client(handler)
    output = await _get_issue_impl(client, "PROJ-1", include_comments=False)
    # The issue still renders; the custom field keeps its raw ID as label.
    assert "# PROJ-1: Add tags to cards" in output
    assert "- **customfield_10101:**" in output


async def test_catalog_failure_is_retried_next_call() -> None:
    handler = Handler(catalog_status=500)
    client = make_client(handler)
    await _get_issue_impl(client, "PROJ-1", include_comments=False)
    handler.catalog_status = 200
    output = await _get_issue_impl(client, "PROJ-1", include_comments=False)
    assert "- **Acceptance Criteria:**" in output
    assert handler.catalog_requests == 2


CONTEXT_ISSUE = {
    "key": "PROJ-1",
    "fields": {
        "summary": "Add tags",
        "issuetype": {"name": "Story"},
        "status": {"name": "To Do"},
        "subtasks": [],
        "issuelinks": [],
    },
}

REMOTE_LINKS = [
    {
        "relationship": "mentioned in",
        "application": {"type": "com.atlassian.confluence", "name": "Confluence"},
        "object": {
            "url": f"{SITE}/wiki/pages/viewpage.action?pageId=4242",
            "title": "Tagging spec",
        },
    },
    {"object": {"url": "https://github.com/x/y/pull/1", "title": "PR #1"}},
]


def context_handler(remote_status: int) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json=ACCOUNT)
        if path == "/rest/api/3/issue/PROJ-1":
            return httpx.Response(200, json=CONTEXT_ISSUE)
        if path == "/rest/api/3/issue/PROJ-1/remotelink":
            if remote_status != 200:
                return httpx.Response(remote_status)
            return httpx.Response(200, json=REMOTE_LINKS)
        return httpx.Response(404)

    return handler


async def test_context_lists_confluence_remote_links_with_page_id() -> None:
    client = make_client(context_handler(200))
    output = await _get_issue_context_impl(client, "PROJ-1")
    assert "## Remote links (2)" in output
    assert (
        f"- mentioned in: [Tagging spec]({SITE}/wiki/pages/viewpage.action?pageId=4242)"
        " — Confluence page id 4242 (get_confluence_page)" in output
    )
    assert "- [PR #1](https://github.com/x/y/pull/1)" in output


async def test_context_survives_remote_link_failure() -> None:
    client = make_client(context_handler(403))
    output = await _get_issue_context_impl(client, "PROJ-1")
    assert "## Linked issues" in output
    assert "Remote links" not in output
