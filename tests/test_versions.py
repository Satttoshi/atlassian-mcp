"""Tests for the release version tools (mocked HTTP with a small fake Jira)."""

import json
import re
from datetime import date
from typing import Any

import httpx
import pytest
from fastmcp import FastMCP

from jira_mcp import versions, writes
from jira_mcp.client import (
    AtlassianError,
    JiraClient,
    JiraWriteClient,
    ReadOnlyViolationError,
    WriteNotAllowedError,
)
from jira_mcp.config import Settings, Site, _parse_projects

SITE = "https://example.atlassian.net"
PROJECT_IDS = {"OM": "100", "APP": "200", "EC": "300"}
TODAY = date(2026, 11, 20)
ARI = "ari:cloud:jira:cloud-1:version/activation/act-1"
NOTES = "## Features\n- one ([OM-1](https://x.io/OM-1))\n- two\n\nDone."


class FakeJira:
    """Versions and fix versions in memory; records every non-GET request."""

    def __init__(self) -> None:
        self.versions: dict[str, dict[str, Any]] = {}
        self.fix: dict[str, list[str]] = {}
        self.writes: list[tuple[str, str, Any]] = []
        self.permissions: dict[str, bool] = {}
        self.fail: dict[tuple[str, str], httpx.Response] = {}
        self.moved: dict[str, str] = {}
        self.sections: dict[str, dict[str, Any]] = {}
        self.related: dict[str, list[dict[str, Any]]] = {}
        self.graphql_error: str | None = None
        self._next_id = 5000

    def add_version(self, project: str, name: str, **fields: Any) -> str:
        self._next_id += 1
        version_id = str(self._next_id)
        self.versions[version_id] = {
            "id": version_id,
            "name": name,
            "projectId": int(PROJECT_IDS[project]),
            "released": False,
            "archived": False,
            **fields,
        }
        return version_id

    def of_project(self, project: str) -> list[dict[str, Any]]:
        wanted = int(PROJECT_IDS[project])
        return [v for v in self.versions.values() if v["projectId"] == wanted]

    def names(self, key: str) -> list[str]:
        return [self.versions[i]["name"] for i in self.fix[key]]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        params = request.url.params
        body: Any = json.loads(request.content) if request.content else None
        if method != "GET":
            self.writes.append((method, path, body))
        if (method, path) in self.fail:
            return self.fail[(method, path)]
        if path == "/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": "cloud-1"})
        if path == "/gateway/api/graphql":
            return self._graphql(body)
        if m := re.fullmatch(r"/rest/api/3/version/(\d+)/relatedwork", path):
            entries = self.related.setdefault(m[1], [])
            if method == "POST":
                entries.append(body)
            return httpx.Response(200, json=entries)
        if path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "abc"})
        if path == "/rest/api/3/field":
            return httpx.Response(
                200, json=[{"id": "fixVersions", "name": "Fix versions"}]
            )
        if path == "/rest/api/3/user/assignable/search":
            return httpx.Response(
                200, json=[{"accountId": "acc-ada", "displayName": "Ada Lovelace"}]
            )
        if path == "/rest/api/3/user":
            return httpx.Response(200, json={"displayName": "Ada Lovelace"})
        if path == "/rest/api/3/mypermissions":
            name = params["permissions"]
            have = self.permissions.get(name, True)
            return httpx.Response(
                200, json={"permissions": {name: {"havePermission": have}}}
            )
        if m := re.fullmatch(r"/rest/api/3/project/(\w+)", path):
            if m[1] not in PROJECT_IDS:
                return httpx.Response(404)
            return httpx.Response(200, json={"id": PROJECT_IDS[m[1]], "key": m[1]})
        if m := re.fullmatch(r"/rest/api/3/project/(\w+)/versions", path):
            if m[1] not in PROJECT_IDS:
                return httpx.Response(404)
            # Like Jira: this endpoint never returns the driver.
            plain = [
                {k: v for k, v in version.items() if k != "driver"}
                for version in self.of_project(m[1])
            ]
            return httpx.Response(200, json=plain)
        if m := re.fullmatch(r"/rest/api/3/project/(\w+)/version", path):
            return self._page(m[1], params)
        if path == "/rest/api/3/version" and method == "POST":
            project = next(
                k for k, v in PROJECT_IDS.items() if int(v) == body["projectId"]
            )
            extra = {k: v for k, v in body.items() if k not in ("name", "projectId")}
            version_id = self.add_version(project, body["name"], **extra)
            return httpx.Response(201, json=self.versions[version_id])
        if m := re.fullmatch(r"/rest/api/3/version/(\d+)/unresolvedIssueCount", path):
            return httpx.Response(
                200, json={"issuesUnresolvedCount": 2, "issuesCount": 5}
            )
        if (m := re.fullmatch(r"/rest/api/3/version/(\d+)", path)) and method == "GET":
            return httpx.Response(200, json=self.versions[m[1]])
        if (m := re.fullmatch(r"/rest/api/3/version/(\d+)", path)) and method == "PUT":
            self.versions[m[1]].update(body)
            return httpx.Response(200, json=self.versions[m[1]])
        if m := re.fullmatch(r"/rest/api/[23]/issue/([A-Z]+-\d+)", path):
            key = m[1]
            if key not in self.fix:
                return httpx.Response(404)
            if method == "GET":
                fix = [self.versions[i] for i in self.fix[key]]
                return httpx.Response(
                    200,
                    json={
                        "key": self.moved.get(key, key),
                        "fields": {"fixVersions": fix},
                    },
                )
            for op in body["update"]["fixVersions"]:
                if "add" in op:
                    self.fix[key].append(op["add"]["id"])
                else:
                    self.fix[key].remove(op["remove"]["id"])
            return httpx.Response(204)
        return httpx.Response(404)

    def _graphql(self, body: Any) -> httpx.Response:
        if self.graphql_error:
            return httpx.Response(
                200, json={"data": None, "errors": [{"message": self.graphql_error}]}
            )
        query, variables = body["query"], body["variables"]
        if "versionSearch" in query:
            assert variables == {"cloudId": "cloud-1", "projects": ["OM"]}
            node = {"id": f"{ARI}/1"}
            return httpx.Response(
                200,
                json={"data": {"jira": {"versionSearch": {"edges": [{"node": node}]}}}},
            )
        if "mutation" in query:
            assert '@optIn(to: "JiraUpdateVersionRichTextSection' in query
        if "versionById" in query:
            assert "@optIn" in query
            version_id = variables["id"].removeprefix(f"{ARI}/")
            section = self.sections.get(version_id, {})
            data = {
                "versionId": version_id,
                "richTextSection": {
                    "title": section.get("title"),
                    "content": {"json": section.get("content")},
                },
            }
            return httpx.Response(200, json={"data": {"jira": {"versionById": data}}})
        payload = variables["input"]
        version_id = payload.pop("id").removeprefix(f"{ARI}/")
        self.sections.setdefault(version_id, {}).update(payload)
        field = next(f for f in ("SectionTitle", "SectionContent") if f in query)
        return httpx.Response(
            200,
            json={
                "data": {
                    "jira": {
                        f"updateJiraVersionRichText{field}": {
                            "success": True,
                            "errors": None,
                        }
                    }
                }
            },
        )

    def _page(self, project: str, params: httpx.QueryParams) -> httpx.Response:
        found = list(reversed(self.of_project(project)))
        if query := params.get("query"):
            found = [
                v
                for v in found
                if query in (v["name"] + v.get("description", "")).lower()
            ]
        if status := params.get("status"):
            found = [
                v
                for v in found
                if (status == "archived" and v["archived"])
                or (status == "released" and v["released"] and not v["archived"])
                or (status == "unreleased" and not v["released"] and not v["archived"])
            ]
        start, size = int(params["startAt"]), int(params["maxResults"])
        values = found[start : start + size]
        return httpx.Response(
            200, json={"values": values, "isLast": start + size >= len(found)}
        )


def make(jira: FakeJira, projects: str = "OM,APP") -> JiraWriteClient:
    settings = Settings(
        email="ada@x.io",
        sites=(Site("example", SITE),),
        api_token="secret",
        cloud_id=None,
        env_file_found=True,
        jira_write_projects=_parse_projects(projects),
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(jira))
    return JiraWriteClient(settings, http=http)


def report(project: str, version_id: str) -> str:
    return (
        f"{SITE}/projects/{project}/versions/{version_id}/tab/release-report-all-issues"
    )


async def tag(
    jira: FakeJira,
    keys: list[str],
    version: str = "mytrips-3.70.0",
    action: versions.Action = "add",
    create_missing: bool = False,
    projects: str = "OM,APP",
) -> str:
    client = make(jira, projects)

    async def client_for(project: str) -> JiraWriteClient:
        return client

    return await versions.set_fix_version_impl(
        client_for, client.projects, keys, version, action, create_missing
    )


# -- client guard -------------------------------------------------------------------


async def test_version_writes_need_a_looked_up_project() -> None:
    jira = FakeJira()
    client = make(jira)
    version_id = jira.add_version("OM", "v1")
    foreign_id = jira.add_version("EC", "v1")
    with pytest.raises(WriteNotAllowedError, match="not looked up"):
        await client.send(
            "PUT", f"/rest/api/3/version/{version_id}", {"released": True}
        )
    with pytest.raises(WriteNotAllowedError, match="not looked up"):
        await client.send(
            "POST", "/rest/api/3/version", {"name": "x", "projectId": 100}
        )
    await client.project_versions("EC")
    await client.project_id("EC")
    with pytest.raises(WriteNotAllowedError, match="EC is not in"):
        await client.send(
            "PUT", f"/rest/api/3/version/{foreign_id}", {"released": True}
        )
    with pytest.raises(WriteNotAllowedError, match="EC is not in"):
        await client.send(
            "POST", "/rest/api/3/version", {"name": "x", "projectId": 300}
        )
    assert jira.writes == []


@pytest.mark.parametrize(
    "body",
    [{"name": "renamed"}, {"archived": True}, {"moveUnfixedIssuesTo": 1}],
)
async def test_version_update_can_not_rename_archive_or_move(
    body: dict[str, Any],
) -> None:
    jira = FakeJira()
    client = make(jira)
    version_id = jira.add_version("OM", "v1")
    await client.project_versions("OM")
    with pytest.raises(WriteNotAllowedError, match="may not be set"):
        await client.send("PUT", f"/rest/api/3/version/{version_id}", body)
    with pytest.raises(WriteNotAllowedError):
        await client.send("DELETE", f"/rest/api/3/version/{version_id}")
    assert jira.writes == []


# -- list_versions ------------------------------------------------------------------


async def test_list_versions_newest_first_with_report_url() -> None:
    jira = FakeJira()
    old = jira.add_version(
        "OM",
        "mytrips-3.69.0",
        released=True,
        releaseDate="2026-11-10",
        startDate="2026-11-03",
        description="November release",
    )
    jira.add_version("OM", "account-1.0.0", description="not mytrips-related")
    new = jira.add_version("OM", "mytrips-3.70.0")
    out = await versions.list_versions_impl(make(jira), "om", query="MyTrips-")
    assert out.index("mytrips-3.70.0") < out.index("mytrips-3.69.0")
    assert "account-1.0.0" not in out
    assert (
        f"**mytrips-3.69.0** (id {old}): released: yes, archived: no, "
        "start: 2026-11-03, release: 2026-11-10" in out
    )
    assert "November release" in out
    assert report("OM", old) in out and report("OM", new) in out


async def test_list_versions_status_limit_and_empty() -> None:
    jira = FakeJira()
    for n in range(60):
        jira.add_version("OM", f"v{n}", released=n % 2 == 0)
    client = make(jira)
    out = await versions.list_versions_impl(client, "OM", status="released", limit=3)
    assert [line for line in out.splitlines() if line.startswith("- ")] == [
        line for line in out.splitlines() if "released: yes" in line
    ]
    assert out.count("- **") == 3 and "v58" in out and "More versions exist" in out
    everything = await versions.list_versions_impl(client, "OM", limit=100)
    assert everything.count("- **") == 60 and "More versions" not in everything
    assert "No versions in APP" in await versions.list_versions_impl(client, "APP")
    with pytest.raises(ValueError, match="project key"):
        await versions.list_versions_impl(client, "OM/../x")


# -- create_version -----------------------------------------------------------------


async def test_create_version_is_idempotent() -> None:
    jira = FakeJira()
    client = make(jira)
    first = await versions.create_version_impl(
        client, "om", "mytrips-3.70.0", "desc", "2026-11-20", "2026-11-27"
    )
    assert jira.writes == [
        (
            "POST",
            "/rest/api/3/version",
            {
                "name": "mytrips-3.70.0",
                "projectId": 100,
                "description": "desc",
                "startDate": "2026-11-20",
                "releaseDate": "2026-11-27",
            },
        )
    ]
    version_id = jira.of_project("OM")[0]["id"]
    assert first.startswith("Created version 'mytrips-3.70.0'")
    assert report("OM", version_id) in first
    again = await versions.create_version_impl(
        client, "OM", "mytrips-3.70.0", release_date="2027-01-01"
    )
    assert "Already exists, nothing changed" in again and version_id in again
    assert len(jira.writes) == 1 and len(jira.of_project("OM")) == 1


async def test_create_version_rejects_bad_input() -> None:
    jira = FakeJira()
    jira.add_version("OM", "MyTrips-1.0.0")
    client = make(jira)
    with pytest.raises(ValueError, match="only in case"):
        await versions.create_version_impl(client, "OM", "mytrips-1.0.0")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await versions.create_version_impl(client, "OM", "v", start_date="20.11.2026")
    with pytest.raises(ValueError, match="longer than 255"):
        await versions.create_version_impl(client, "OM", "v" * 256)
    with pytest.raises(WriteNotAllowedError, match="EC is not in"):
        await versions.create_version_impl(client, "EC", "v")
    assert jira.writes == []


async def test_create_version_names_the_missing_permission() -> None:
    jira = FakeJira()
    jira.fail[("POST", "/rest/api/3/version")] = httpx.Response(403)
    jira.permissions["ADMINISTER_PROJECTS"] = False
    with pytest.raises(
        Exception,
        match="cannot create versions in OM: missing permission Administer Projects",
    ):
        await versions.create_version_impl(make(jira), "OM", "v1")
    jira.permissions["ADMINISTER_PROJECTS"] = True
    with pytest.raises(Exception, match="manage:jira-project.*releases are turned off"):
        await versions.create_version_impl(make(jira), "OM", "v1")


# -- update_version -----------------------------------------------------------------


async def test_update_version_marks_released_today_and_is_a_noop_after() -> None:
    jira = FakeJira()
    version_id = jira.add_version("OM", "mytrips-3.70.0", description="d")
    client = make(jira)
    out = await versions.update_version_impl(
        client, "OM", "mytrips-3.70.0", released=True, today=TODAY
    )
    path = f"/rest/api/3/version/{version_id}"
    assert jira.writes == [
        ("PUT", path, {"released": True, "releaseDate": "2026-11-20"})
    ]
    assert "Updated version 'mytrips-3.70.0' in OM: releaseDate, released." in out
    assert "Released: yes, release date: 2026-11-20" in out
    assert "2 of 5 issues unresolved (none were moved)" in out
    assert report("OM", version_id) in out

    later = await versions.update_version_impl(
        client, "OM", "mytrips-3.70.0", released=True, today=date(2026, 12, 1)
    )
    assert "nothing changed" in later and "release date: 2026-11-20" in later
    assert len(jira.writes) == 1


async def test_update_version_changes_only_passed_fields() -> None:
    jira = FakeJira()
    version_id = jira.add_version("OM", "v1", releaseDate="2026-11-27")
    client = make(jira)
    await versions.update_version_impl(
        client, "OM", "v1", released=True, start_date="2026-11-20", today=TODAY
    )
    await versions.update_version_impl(client, "OM", "v1", description="new")
    assert [body for _, _, body in jira.writes] == [
        {"released": True, "startDate": "2026-11-20"},
        {"description": "new"},
    ]
    assert jira.versions[version_id]["releaseDate"] == "2026-11-27"
    with pytest.raises(ValueError, match="no version named 'missing'"):
        await versions.update_version_impl(client, "OM", "missing", released=True)


async def test_driver_is_resolved_set_once_and_listed() -> None:
    jira = FakeJira()
    client = make(jira)
    await versions.create_version_impl(client, "OM", "v1", driver="Ada Lovelace")
    jira.add_version("OM", "v2")
    out = await versions.update_version_impl(client, "OM", "v2", driver="ada lovelace")
    assert "Updated version 'v2' in OM: driver." in out
    again = await versions.update_version_impl(
        client, "OM", "v2", driver="Ada Lovelace"
    )
    assert "nothing changed" in again
    assert [body for _, _, body in jira.writes] == [
        {"name": "v1", "projectId": 100, "driver": "acc-ada"},
        {"driver": "acc-ada"},
    ]
    with pytest.raises(ValueError, match="not removed"):
        await versions.update_version_impl(client, "OM", "v2", driver="none")
    jira.add_version("OM", "v3")
    listed = await versions.list_versions_impl(client, "OM")
    assert listed.count("driver: Ada Lovelace") == 2
    assert listed.count("driver: none") == 1
    assert "- Driver: Ada Lovelace" in await versions.get_version_impl(
        client, "OM", "v2"
    )
    assert "- Driver: none" in await versions.get_version_impl(client, "OM", "v3")


# -- set_fix_version ----------------------------------------------------------------


async def test_set_fix_version_adds_and_keeps_existing_versions() -> None:
    jira = FakeJira()
    old = jira.add_version("OM", "mytrips-3.69.0")
    new = jira.add_version("OM", "mytrips-3.70.0")
    jira.fix = {"OM-1": [old], "OM-2": [new], "APP-7": []}
    out = await tag(jira, ["om-1", "OM-2", "APP-7", "OM-1"], create_missing=True)
    created = jira.of_project("APP")[0]["id"]
    assert jira.names("OM-1") == ["mytrips-3.69.0", "mytrips-3.70.0"]
    assert jira.names("APP-7") == ["mytrips-3.70.0"]
    assert (
        "PUT",
        "/rest/api/2/issue/OM-1",
        {"update": {"fixVersions": [{"add": {"id": new}}]}},
    ) in jira.writes
    assert out.splitlines()[:5] == [
        "Fix version 'mytrips-3.70.0': add on 3 issue(s)",
        "",
        "- OM-1: added",
        "- OM-2: unchanged",
        "- APP-7: added",
    ]
    assert f"- APP (version created): {report('APP', created)}" in out
    assert f"- OM: {report('OM', new)}" in out
    assert out.endswith("Result: 2 added, 1 unchanged.")

    before = len(jira.writes)
    again = await tag(jira, ["OM-1", "OM-2", "APP-7"], create_missing=True)
    assert again.endswith("Result: 3 unchanged.") and len(jira.writes) == before


async def test_set_fix_version_partial_success() -> None:
    jira = FakeJira()
    jira.add_version("OM", "mytrips-3.70.0")
    jira.add_version("EC", "mytrips-3.70.0")
    jira.fix = {"OM-1": [], "EC-5": [], "APP-7": [], "OM-9": []}
    jira.moved["OM-9"] = "EC-77"
    out = await tag(jira, ["OM-1", "EC-5", "APP-7", "OM-404", "nonsense", "OM-9"])
    assert "- OM-1: added" in out
    assert "- EC-5: skipped (project not writable: EC is not in" in out
    assert (
        "- APP-7: skipped (APP has no version 'mytrips-3.70.0'; pass "
        "create_missing=true to create it)" in out
    )
    assert "- OM-404: failed (issue not found or not visible to this account)" in out
    assert "- nonsense: failed (not a Jira issue key)" in out
    assert "- OM-9: skipped (moved to another project: EC-77)" in out
    assert out.endswith("Result: 1 added, 3 skipped, 2 failed.")
    assert jira.fix["EC-5"] == [] and jira.of_project("APP") == []


async def test_set_fix_version_remove_only_that_version() -> None:
    jira = FakeJira()
    old = jira.add_version("OM", "mytrips-3.69.0")
    new = jira.add_version("OM", "mytrips-3.70.0")
    jira.fix = {"OM-1": [old, new], "OM-2": [old], "APP-7": []}
    out = await tag(
        jira, ["OM-1", "OM-2", "APP-7"], action="remove", create_missing=True
    )
    assert jira.fix == {"OM-1": [old], "OM-2": [old], "APP-7": []}
    assert "- OM-1: removed" in out and "- OM-2: unchanged" in out
    assert "- APP-7: unchanged (APP has no version 'mytrips-3.70.0')" in out
    assert jira.of_project("APP") == []


async def test_set_fix_version_archived_and_limits() -> None:
    jira = FakeJira()
    jira.add_version("OM", "mytrips-3.70.0", archived=True)
    jira.fix = {"OM-1": []}
    out = await tag(jira, ["OM-1"])
    assert "- OM-1: skipped (version 'mytrips-3.70.0' is archived in OM)" in out
    with pytest.raises(ValueError, match="At most 100"):
        await tag(jira, [f"OM-{n}" for n in range(101)])
    with pytest.raises(ValueError, match="issue_keys is empty"):
        await tag(jira, [])
    assert jira.writes == []


async def test_set_fix_version_explains_issue_failures() -> None:
    jira = FakeJira()
    jira.add_version("OM", "mytrips-3.70.0")
    jira.fix = {"OM-1": []}
    jira.fail[("PUT", "/rest/api/2/issue/OM-1")] = httpx.Response(
        400,
        json={
            "errorMessages": [],
            "errors": {"fixVersions": "Field 'fixVersions' cannot be set."},
        },
    )
    jira.permissions["RESOLVE_ISSUES"] = False
    assert "- OM-1: failed (missing permission Resolve Issues)" in await tag(
        jira, ["OM-1"]
    )
    jira.permissions["RESOLVE_ISSUES"] = True
    out = await tag(jira, ["OM-1"])
    assert "- OM-1: failed (Fix versions is not on the edit screen of OM-1." in out


async def test_set_fix_version_reports_version_create_failure_per_issue() -> None:
    jira = FakeJira()
    jira.add_version("OM", "mytrips-3.70.0")
    jira.fix = {"OM-1": [], "APP-7": []}
    jira.fail[("POST", "/rest/api/3/version")] = httpx.Response(403)
    jira.permissions["ADMINISTER_PROJECTS"] = False
    out = await tag(jira, ["OM-1", "APP-7"], create_missing=True)
    assert "- OM-1: added" in out
    assert (
        "- APP-7: failed (cannot create versions in APP: missing permission "
        "Administer Projects.)" in out
    )


# -- release page: section and related work -----------------------------------------


async def test_section_is_written_then_left_alone_then_replaced() -> None:
    jira = FakeJira()
    version_id = jira.add_version("OM", "v1")
    client = make(jira)
    out = await versions.set_version_section_impl(
        client, "OM", "v1", "Release notes", NOTES
    )
    assert "Wrote the section of 'v1' in OM: title, content." in out
    assert report("OM", version_id) in out
    stored = jira.sections[version_id]
    assert stored["title"] == "Release notes"
    assert [node["type"] for node in stored["content"]["content"]] == [
        "heading",
        "bulletList",
        "paragraph",
    ]
    graphql_writes = len(jira.writes)

    kept = await versions.set_version_section_impl(client, "OM", "v1", "X", "other")
    assert "Left the section of 'v1' in OM alone" in kept
    assert "Existing title: Release notes" in kept
    assert "> - one ([OM-1](https://x.io/OM-1))" in kept
    assert jira.sections[version_id]["title"] == "Release notes"

    same = await versions.set_version_section_impl(
        client, "OM", "v1", "Release notes", NOTES, "replace"
    )
    assert "nothing changed" in same
    # Reads are GraphQL POSTs too; only the first call sent the two mutations.
    mutations = [b for _, _, b in jira.writes if "mutation" in b.get("query", "")]
    assert len(mutations) == 2 and graphql_writes >= 2

    replaced = await versions.set_version_section_impl(
        client, "OM", "v1", content="new text", mode="replace"
    )
    assert "Wrote the section of 'v1' in OM: content." in replaced
    assert jira.sections[version_id]["title"] == "Release notes"
    shown = await versions.get_version_impl(client, "OM", "v1")
    assert "**Section:** Release notes" in shown and "new text" in shown


async def test_section_failures_are_plain_and_guarded() -> None:
    jira = FakeJira()
    version_id = jira.add_version("OM", "v1")
    foreign = jira.add_version("EC", "v1")
    client = make(jira)
    with pytest.raises(ValueError, match="Nothing to write"):
        await versions.set_version_section_impl(client, "OM", "v1")
    with pytest.raises(ValueError, match="no version named"):
        await versions.set_version_section_impl(client, "OM", "nope", "t")
    with pytest.raises(WriteNotAllowedError, match="EC is not in"):
        await versions.set_version_section_impl(client, "EC", "v1", "t")
    mutation = {"input": {"id": f"{ARI}/{foreign}", "title": "t"}}
    with pytest.raises(WriteNotAllowedError, match="not looked up"):
        await client.graphql("SetVersionSectionTitle", mutation)
    await client.project_versions("EC")
    with pytest.raises(WriteNotAllowedError, match="EC is not in"):
        await client.graphql("SetVersionSectionTitle", mutation)
    with pytest.raises(ReadOnlyViolationError):
        await client.graphql("DeleteEverything", {})
    with pytest.raises(ReadOnlyViolationError):
        await JiraClient(client._settings).graphql("SetVersionSectionTitle", mutation)

    jira.graphql_error = "This API is not available with API tokens"
    with pytest.raises(
        AtlassianError,
        match="can't be reached with this token.*"
        "nothing was written, also not to the description.*"
        "not available with API tokens",
    ):
        await versions.set_version_section_impl(client, "OM", "v1", "t", "c")
    assert version_id not in jira.sections
    shown = await versions.get_version_impl(client, "OM", "v1")
    assert "**Section:** could not be read" in shown


async def test_related_work_is_added_once() -> None:
    jira = FakeJira()
    version_id = jira.add_version("OM", "v1")
    client = make(jira)
    url = "https://github.com/acme/app/releases/tag/v1"
    out = await versions.add_version_related_work_impl(
        client, "OM", "v1", "GitHub Release", url
    )
    assert (
        f"Added related work to 'v1' in OM: [Communication] GitHub Release: {url}."
        in out
    )
    again = await versions.add_version_related_work_impl(
        client, "OM", "v1", "Other title", url + "/", "Development"
    )
    assert "Already linked on 'v1' in OM, nothing changed" in again
    assert jira.related[version_id] == [
        {"category": "Communication", "title": "GitHub Release", "url": url}
    ]
    shown = await versions.get_version_impl(client, "OM", "v1")
    assert f"- [Communication] GitHub Release: {url}" in shown
    with pytest.raises(ValueError, match="http"):
        await versions.add_version_related_work_impl(client, "OM", "v1", "t", "x.io")
    with pytest.raises(WriteNotAllowedError, match="may not be set"):
        await client.send(
            "POST",
            f"/rest/api/3/version/{version_id}/relatedwork",
            {"category": "c", "title": "t", "url": url, "issueId": 1},
        )


async def test_list_versions_shows_first_description_line_only() -> None:
    jira = FakeJira()
    jira.add_version("OM", "v1", description="Short line\n\n- long\n- notes")
    out = await versions.list_versions_impl(make(jira), "OM")
    assert "  Short line …" in out and "- long" not in out


# -- update_issue and registration --------------------------------------------------


async def test_update_issue_refuses_fix_versions() -> None:
    jira = FakeJira()
    with pytest.raises(ValueError, match="Use set_fix_version"):
        await writes.update_issue_impl(
            make(jira), "OM-1", fields={"Fix versions": [{"name": "v1"}]}
        )
    assert jira.writes == []


async def test_register_adds_tools_and_instructions() -> None:
    jira = FakeJira()
    jira.fix = {"OM-1": []}
    client = make(jira)
    mcp = FastMCP(
        name="t",
        instructions="Constraints: strictly read-only; nothing can be created, "
        "edited, commented on, or transitioned.",
    )
    routed: list[tuple[str | None, str | None]] = []

    async def client_for(
        site: str | None,
        *,
        issue_key: str | None = None,
        link_id: str | int | None = None,
    ) -> JiraWriteClient:
        routed.append((site, issue_key))
        return client

    writes.register(mcp, client_for, client.projects)
    versions.register(mcp, client_for, client.projects)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert {
        "create_version",
        "update_version",
        "set_fix_version",
        "set_version_section",
        "add_version_related_work",
    } <= set(tools)
    assert all("site" in tool.parameters["properties"] for tool in tools.values())
    instructions = mcp.instructions or ""
    assert "enabled for Jira projects APP, OM only" in instructions
    assert "create_version, update_version, set_fix_version" in instructions

    await mcp.call_tool("create_version", {"project": "om", "name": "v1"})
    await mcp.call_tool(
        "set_fix_version",
        {"issue_keys": ["OM-1"], "version": "v1", "site": "example"},
    )
    await mcp.call_tool(
        "update_version", {"project": "OM", "name": "v1", "released": True}
    )
    assert routed == [(None, "OM-0"), ("example", "OM-0"), (None, "OM-0")]
    assert jira.names("OM-1") == ["v1"]
    assert jira.of_project("OM")[0]["released"] is True
