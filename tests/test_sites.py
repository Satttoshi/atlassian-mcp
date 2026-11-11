"""Tests for multi-site routing in the MCP tools."""

import dataclasses
from collections.abc import Iterator

import httpx
import pytest
from fastmcp.exceptions import ToolError

from jira_mcp import server
from jira_mcp.client import JiraClient
from jira_mcp.config import Settings, Site

ACME = Site("acme", "https://acme.atlassian.net")
OTHER = Site("other", "https://other.atlassian.net")
ACCOUNT = {"accountId": "abc", "displayName": "Ada"}

# Which projects each site has; SHARED exists on both.
PROJECTS = {"acme": {"ACME", "SHARED"}, "other": {"OTH", "SHARED"}}


def make_settings(*sites: Site, jira_only: bool = False) -> Settings:
    return Settings(
        email="ada@x.io",
        sites=sites,
        cloud_id=None,
        env_file_found=True,
        api_token=None if jira_only else "secret",
        jira_api_token="jira-secret" if jira_only else None,
    )


def handler(request: httpx.Request) -> httpx.Response:
    site = (request.url.host or "").split(".")[0]
    path = request.url.path
    if path in ("/rest/api/3/myself", "/wiki/rest/api/user/current"):
        return httpx.Response(200, json=ACCOUNT)
    if path == "/rest/api/3/field":
        return httpx.Response(200, json=[])
    if path.startswith("/rest/api/3/project/"):
        known = path.rpartition("/")[2] in PROJECTS[site]
        return httpx.Response(200 if known else 404, json={})
    if path.startswith("/rest/api/3/issue/"):
        key = path.rpartition("/")[2]
        issue = {"key": key, "fields": {"summary": f"Issue on {site}"}}
        return httpx.Response(200, json=issue)
    if path == "/rest/api/3/search/jql":
        return httpx.Response(200, json={"issues": []})
    return httpx.Response(404)


@pytest.fixture
def sites(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[httpx.Request]]:
    """Two configured sites backed by one mock transport; yields the requests."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    settings = make_settings(ACME, OTHER)
    clients = {
        site.name: JiraClient(
            settings,
            http=httpx.AsyncClient(transport=httpx.MockTransport(recording)),
            site=site,
        )
        for site in settings.sites
    }
    monkeypatch.setattr(server, "_settings", settings)
    monkeypatch.setattr(server, "_clients", clients)
    monkeypatch.setattr(server, "_confluence_clients", {})
    monkeypatch.setattr(server, "_project_sites", {})
    monkeypatch.setattr(server, "_field_names", {})
    yield requests


def project_lookups(requests: list[httpx.Request]) -> int:
    return sum("/project/" in r.url.path for r in requests)


async def test_issue_key_is_routed_to_the_site_with_its_project(
    sites: list[httpx.Request],
) -> None:
    output = await server.get_issue("OTH-7", include_comments=False)
    assert output.startswith(
        '**Site:** other (https://other.atlassian.net); pass site="other"'
    )
    assert "Issue on other" in output


async def test_project_lookup_is_cached(sites: list[httpx.Request]) -> None:
    await server.get_issue("OTH-7", include_comments=False)
    await server.get_issue("OTH-8", include_comments=False)
    assert project_lookups(sites) == 2  # one per site, first call only


async def test_project_on_several_sites_needs_explicit_site(
    sites: list[httpx.Request],
) -> None:
    with pytest.raises(ToolError, match="several sites: acme, other"):
        await server.get_issue("SHARED-1", include_comments=False)
    output = await server.get_issue("SHARED-1", include_comments=False, site="other")
    assert "Issue on other" in output


async def test_unknown_project_is_an_error_not_a_guess(
    sites: list[httpx.Request],
) -> None:
    with pytest.raises(ToolError, match="No configured site .* 'NOPE'"):
        await server.get_issue("NOPE-1", include_comments=False)


async def test_explicit_site_skips_the_project_lookup(
    sites: list[httpx.Request],
) -> None:
    await server.get_issue("ACME-1", include_comments=False, site="acme")
    assert project_lookups(sites) == 0


async def test_search_defaults_to_the_first_site(sites: list[httpx.Request]) -> None:
    assert (await server.search_issues("project = X")).startswith("**Site:** acme ")
    output = await server.search_issues("project = X", site="other")
    assert output.startswith("**Site:** other ")


async def test_bare_ids_need_a_site_when_several_are_configured(
    sites: list[httpx.Request],
) -> None:
    with pytest.raises(ToolError, match="site="):
        await server.get_attachment("10023")
    with pytest.raises(ToolError, match="site="):
        await server.get_confluence_page("123456")


async def test_unconfigured_url_host_is_rejected(sites: list[httpx.Request]) -> None:
    url = "https://third.atlassian.net/wiki/spaces/ENG/pages/1/T"
    with pytest.raises(ToolError, match="not a configured site"):
        await server.get_confluence_page(url)


async def test_whoami_reports_every_site_and_token_source(
    sites: list[httpx.Request],
) -> None:
    output = await server.whoami()
    assert "# Site: acme (https://acme.atlassian.net)" in output
    assert "# Site: other (https://other.atlassian.net)" in output
    assert output.count("**Token from:** ATLASSIAN_API_TOKEN") >= 2


async def test_single_site_is_unlabelled_and_needs_no_lookup(
    sites: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_settings", make_settings(ACME))
    output = await server.get_issue("ANY-1", include_comments=False)
    assert "**Site:**" not in output
    assert project_lookups(sites) == 0


async def test_whoami_reports_a_product_without_token_as_not_configured(
    sites: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_settings", make_settings(ACME, jira_only=True))
    output = await server.whoami()
    assert "## Jira\n\n**Account:** Ada" in output
    assert "## Confluence\n\n**Not configured:**" in output


async def test_writes_are_routed_like_reads(
    sites: list[httpx.Request], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = dataclasses.replace(
        server._get_settings(), jira_write_projects=frozenset({"OTH"})
    )
    monkeypatch.setattr(server, "_settings", settings)
    monkeypatch.setattr(server, "_write_clients", {})
    client = await server._write_client_for(None, issue_key="OTH-1")
    assert client.site == OTHER
    assert await server._write_client_for(None, issue_key="OTH-2") is client
    assert (await server._write_client_for("acme", link_id=5)).site == ACME
    with pytest.raises(ValueError, match="is an ID"):
        await server._write_client_for(None, link_id=5)
