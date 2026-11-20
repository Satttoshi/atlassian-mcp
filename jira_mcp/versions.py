"""Jira release versions: list them, create and release them, tag issues.

list_versions is read-only and always available. The write tools are opt-in
like the ones in writes.py and share JIRA_WRITE_PROJECTS. Callers pass version
names, never IDs, and every write is idempotent: the release tooling repeats
its calls across sessions.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .client import (
    AtlassianClient,
    AtlassianError,
    JiraWriteClient,
    ReadOnlyViolationError,
    project_of,
)
from .config import ConfigError
from .render import normalize_issue_key
from .writes import ClientFor, resolve_account_id

MAX_NAME_LENGTH = 255
MAX_ISSUES = 100
PAGE_SIZE = 50
CONCURRENCY = 5

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PROJECT_RE = re.compile(r"[A-Z][A-Z0-9_]*")

Status = Literal["released", "unreleased", "archived", "all"]
Action = Literal["add", "remove"]
RESULTS = ("added", "removed", "unchanged", "skipped", "failed")

# -- helpers ------------------------------------------------------------------------


def normalize_project(raw: str) -> str:
    project = raw.strip().upper()
    if not _PROJECT_RE.fullmatch(project):
        raise ValueError(f"'{raw}' does not look like a Jira project key (e.g. OM).")
    return project


def report_url(client: AtlassianClient, project: str, version_id: str) -> str:
    return (
        f"{client.site_url}/projects/{project}/versions/{version_id}"
        "/tab/release-report-all-issues"
    )


def _check_date(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    try:
        if not _DATE_RE.fullmatch(text):
            raise ValueError
        date.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{name} must be a date as YYYY-MM-DD, got {value!r}."
        ) from None
    return text


def _check_name(name: str) -> None:
    if not name.strip():
        raise ValueError("The version name is empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(
            f"The version name is longer than {MAX_NAME_LENGTH} characters."
        )


def _find(versions: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Exact, case-sensitive: the caller owns the name."""
    return next((v for v in versions if v.get("name") == name), None)


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def jira_detail(exc: AtlassianError) -> str:
    """What Jira itself said about a failed call, if anything."""
    try:
        data = json.loads(exc.detail)
    except ValueError:
        return exc.detail.strip()
    if not isinstance(data, dict):
        return exc.detail.strip()
    messages = [str(m) for m in data.get("errorMessages") or []]
    messages += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return " ".join(messages)


async def has_permission(
    client: AtlassianClient, permission: str, **scope: str
) -> bool | None:
    """None when the answer can't be determined."""
    try:
        data = await client.get_json(
            "/rest/api/3/mypermissions", params={"permissions": permission, **scope}
        )
        return bool(data["permissions"][permission]["havePermission"])
    except (AtlassianError, KeyError, TypeError):
        return None


async def explain_version_error(
    client: AtlassianClient, project: str, verb: str, exc: AtlassianError
) -> str:
    """Name the missing permission instead of passing on a bare status code."""
    prefix = f"cannot {verb} versions in {project}"
    said = jira_detail(exc)
    jira_said = f" Jira said: {said}" if said else ""
    if exc.status_code not in (401, 403, 404):
        return f"{prefix}: {said or exc}"
    allowed = await has_permission(client, "ADMINISTER_PROJECTS", projectKey=project)
    if allowed is False:
        return f"{prefix}: missing permission Administer Projects."
    cause = (
        "the account has Administer Projects, so either the API token is scoped "
        "without manage:jira-project, or releases are turned off in this project"
        if allowed
        else "the account may lack the permission Administer Projects, the API "
        "token may be scoped without manage:jira-project, or releases may be "
        "turned off in this project"
    )
    return f"{prefix} (HTTP {exc.status_code}): {cause}.{jira_said}"


async def explain_issue_error(
    client: AtlassianClient, key: str, exc: AtlassianError
) -> str:
    said = jira_detail(exc)
    if exc.status_code == 404:
        return "issue not found or not visible to this account"
    about_field = "fixversions" in said.lower()
    if exc.status_code in (401, 403) or about_field:
        for permission, label in (
            ("RESOLVE_ISSUES", "Resolve Issues"),
            ("EDIT_ISSUES", "Edit Issues"),
        ):
            if await has_permission(client, permission, issueKey=key) is False:
                return f"missing permission {label}"
        if about_field:
            return f"Fix versions is not on the edit screen of {key}. Jira said: {said}"
    return said or str(exc)


async def _driver_id(client: JiraWriteClient, project: str, driver: str) -> str:
    account = await resolve_account_id(client, driver, issue_key=None, project=project)
    if account is None:
        raise ValueError("A version's driver can be changed but not removed here.")
    return account


async def _driver_names(
    client: AtlassianClient, versions: list[dict[str, Any]]
) -> dict[str, str]:
    """Display names for the drivers' account IDs; the ID where lookup fails."""

    async def name(account: str) -> str:
        try:
            user = await client.get_json(
                "/rest/api/3/user", params={"accountId": account}
            )
            return str(user.get("displayName") or account)
        except AtlassianError:
            return account

    accounts = sorted({str(v["driver"]) for v in versions if v.get("driver")})
    names = await asyncio.gather(*(name(a) for a in accounts))
    return dict(zip(accounts, names, strict=True))


# -- list ---------------------------------------------------------------------------


def _render_version(
    client: AtlassianClient, project: str, v: dict[str, Any], drivers: dict[str, str]
) -> str:
    version_id = str(v["id"])
    lines = [
        (
            f"- **{v.get('name')}** (id {version_id}): "
            f"released: {_yes_no(v.get('released'))}, "
            f"archived: {_yes_no(v.get('archived'))}, "
            f"start: {v.get('startDate') or 'none'}, "
            f"release: {v.get('releaseDate') or 'none'}, "
            f"driver: {drivers.get(str(v.get('driver')), 'none')}"
        )
    ]
    if v.get("description"):
        lines.append(f"  {v['description']}")
    lines.append(f"  {report_url(client, project, version_id)}")
    return "\n".join(lines)


async def list_versions_impl(
    client: AtlassianClient,
    project: str,
    query: str | None = None,
    status: Status = "all",
    limit: int = 25,
) -> str:
    project = normalize_project(project)
    if status not in ("released", "unreleased", "archived", "all"):
        raise ValueError("status must be released, unreleased, archived or all.")
    limit = max(1, min(limit, 100))
    wanted = (query or "").strip().lower()
    params: dict[str, Any] = {
        "maxResults": PAGE_SIZE,
        "orderBy": "-sequence",
        "expand": "driver",
    }
    if wanted:
        params["query"] = wanted
    if status != "all":
        params["status"] = status

    found: list[dict[str, Any]] = []
    start, more = 0, False
    while True:
        page = await client.get_json(
            f"/rest/api/3/project/{project}/version",
            params={**params, "startAt": start},
        )
        values = page.get("values") or []
        # Jira's query also matches descriptions; the filter is on the name.
        found += [v for v in values if wanted in str(v.get("name", "")).lower()]
        start += len(values)
        last = bool(page.get("isLast", True)) or not values
        if len(found) > limit or last:
            more = len(found) > limit or not last
            break

    scope = f" matching {query!r}" if wanted else ""
    scope += f" ({status})" if status != "all" else ""
    if not found:
        return f"No versions in {project}{scope}."
    shown = found[:limit]
    lines = [f"## Versions of {project}{scope}, newest first ({len(shown)})", ""]
    drivers = await _driver_names(client, shown)
    lines += [_render_version(client, project, v, drivers) for v in shown]
    if more:
        lines += [
            "",
            f"More versions exist; raise limit (now {limit}) or narrow query.",
        ]
    return "\n".join(lines)


# -- create and update --------------------------------------------------------------


async def ensure_version(
    client: JiraWriteClient,
    project: str,
    name: str,
    extra: dict[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """The version of that name in the project, and whether it was created now."""
    client.require_project(f"{project}-0")
    versions = await client.project_versions(project)
    existing = _find(versions, name)
    if existing is not None:
        return existing, False
    clash = next(
        (v for v in versions if str(v.get("name", "")).lower() == name.lower()), None
    )
    if clash is not None:
        raise ValueError(
            f"{project} has a version {clash['name']!r} that differs from {name!r} "
            "only in case. Jira treats them as the same name; pass the existing one."
        )
    body: dict[str, Any] = {
        "name": name,
        "projectId": int(await client.project_id(project)),
    }
    body.update(extra or {})
    try:
        created = await client.send("POST", "/rest/api/3/version", body)
    except AtlassianError as exc:
        raise AtlassianError(
            await explain_version_error(client, project, "create", exc),
            status_code=exc.status_code,
        ) from exc
    client.track_version(str(created["id"]), project)
    return created, True


async def create_version_impl(
    client: JiraWriteClient,
    project: str,
    name: str,
    description: str | None = None,
    start_date: str | None = None,
    release_date: str | None = None,
    driver: str | None = None,
) -> str:
    project = normalize_project(project)
    _check_name(name)
    client.require_project(f"{project}-0")
    extra = {
        "driver": await _driver_id(client, project, driver) if driver else None,
        "description": description,
        "startDate": _check_date(start_date, "start_date"),
        "releaseDate": _check_date(release_date, "release_date"),
    }
    version, created = await ensure_version(
        client, project, name, {k: v for k, v in extra.items() if v is not None}
    )
    version_id = str(version["id"])
    state = "Created" if created else "Already exists, nothing changed:"
    return (
        f"{state} version {name!r} (id {version_id}) in {project}. "
        f"{report_url(client, project, version_id)}"
    )


async def update_version_impl(
    client: JiraWriteClient,
    project: str,
    name: str,
    released: bool | None = None,
    release_date: str | None = None,
    start_date: str | None = None,
    description: str | None = None,
    driver: str | None = None,
    today: date | None = None,
) -> str:
    project = normalize_project(project)
    client.require_project(f"{project}-0")
    release_date = _check_date(release_date, "release_date")
    start_date = _check_date(start_date, "start_date")
    version = _find(await client.project_versions(project), name)
    if version is None:
        raise ValueError(
            f"{project} has no version named {name!r}. list_versions shows the "
            "existing ones; create_version creates one."
        )
    version_id = str(version["id"])
    # Today only on the step to released, so a repeated call keeps the date.
    if released and release_date is None and not version.get("releaseDate"):
        release_date = (today or datetime.now().astimezone().date()).isoformat()
    wanted = {
        "released": released,
        "releaseDate": release_date,
        "startDate": start_date,
        "description": description,
        "driver": await _driver_id(client, project, driver) if driver else None,
    }
    current = {**version, "released": bool(version.get("released"))}
    changes = {k: v for k, v in wanted.items() if v is not None and current.get(k) != v}
    if changes:
        try:
            version = await client.send(
                "PUT", f"/rest/api/3/version/{version_id}", changes
            ) or {**version, **changes}
        except AtlassianError as exc:
            raise AtlassianError(
                await explain_version_error(client, project, "edit", exc),
                status_code=exc.status_code,
            ) from exc
    try:
        counts = await client.get_json(
            f"/rest/api/3/version/{version_id}/unresolvedIssueCount"
        )
        unresolved = (
            f"{counts.get('issuesUnresolvedCount')} of {counts.get('issuesCount')} "
            "issues unresolved (none were moved)"
        )
    except AtlassianError:
        unresolved = "unresolved issue count unavailable"
    head = (
        f"Updated version {name!r} in {project}: {', '.join(sorted(changes))}."
        if changes
        else f"Version {name!r} in {project} already has these values, nothing changed."
    )
    return (
        f"{head} Released: {_yes_no(version.get('released'))}, "
        f"release date: {version.get('releaseDate') or 'none'}; {unresolved}. "
        f"{report_url(client, project, version_id)}"
    )


# -- fix versions on issues ---------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    key: str
    result: str
    reason: str = ""

    def line(self) -> str:
        return f"- {self.key}: {self.result}" + (
            f" ({self.reason})" if self.reason else ""
        )


@dataclass(frozen=True)
class _Target:
    """What to do with the issues of one project: tag them, or one fixed result."""

    client: JiraWriteClient | None = None
    version: dict[str, Any] | None = None
    created: bool = False
    result: str = ""
    reason: str = ""


ProjectClient = Callable[[str], Awaitable[JiraWriteClient]]


async def _target(
    client_for: ProjectClient,
    projects: frozenset[str],
    project: str,
    name: str,
    action: Action,
    create_missing: bool,
) -> _Target:
    if project not in projects:
        return _Target(
            result="skipped",
            reason=f"project not writable: {project} is not in JIRA_WRITE_PROJECTS",
        )
    try:
        client = await client_for(project)
        if action == "add" and create_missing:
            version, created = await ensure_version(client, project, name)
            target = _Target(client, version, created)
        else:
            found = _find(await client.project_versions(project), name)
            if found is None and action == "remove":
                return _Target(
                    result="unchanged", reason=f"{project} has no version {name!r}"
                )
            if found is None:
                return _Target(
                    result="skipped",
                    reason=f"{project} has no version {name!r}; pass "
                    "create_missing=true to create it",
                )
            target = _Target(client, found)
    except (AtlassianError, ConfigError, ValueError, ReadOnlyViolationError) as exc:
        return _Target(result="failed", reason=str(exc))
    if action == "add" and (target.version or {}).get("archived"):
        return _Target(
            result="skipped", reason=f"version {name!r} is archived in {project}"
        )
    return target


async def _apply(
    client: JiraWriteClient, key: str, version: dict[str, Any], action: Action
) -> Outcome:
    version_id = str(version["id"])
    try:
        issue = await client.get_json(
            f"/rest/api/3/issue/{key}", params={"fields": "fixVersions"}
        )
        actual = str(issue.get("key") or key)
        if project_of(actual) != project_of(key):
            return Outcome(key, "skipped", f"moved to another project: {actual}")
        current = (issue.get("fields") or {}).get("fixVersions") or []
        has = any(str(v.get("id")) == version_id for v in current)
        if has == (action == "add"):
            return Outcome(key, "unchanged")
        await client.send(
            "PUT",
            f"/rest/api/2/issue/{key}",
            {"update": {"fixVersions": [{action: {"id": version_id}}]}},
        )
    except AtlassianError as exc:
        return Outcome(key, "failed", await explain_issue_error(client, key, exc))
    except ReadOnlyViolationError as exc:
        return Outcome(key, "failed", str(exc))
    return Outcome(key, "added" if action == "add" else "removed")


async def set_fix_version_impl(
    client_for: ProjectClient,
    projects: frozenset[str],
    issue_keys: list[str],
    version: str,
    action: Action = "add",
    create_missing: bool = False,
) -> str:
    _check_name(version)
    if action not in ("add", "remove"):
        raise ValueError("action must be add or remove.")
    if not issue_keys:
        raise ValueError("issue_keys is empty.")
    if len(issue_keys) > MAX_ISSUES:
        raise ValueError(
            f"At most {MAX_ISSUES} issues per call, got {len(issue_keys)}."
        )

    order: list[str] = []
    invalid: dict[str, Outcome] = {}
    for raw in issue_keys:
        try:
            key = normalize_issue_key(raw)
        except ValueError:
            key = raw
            invalid[key] = Outcome(key, "failed", "not a Jira issue key")
        if key not in order:
            order.append(key)

    by_project: dict[str, list[str]] = {}
    for key in order:
        if key not in invalid:
            by_project.setdefault(project_of(key), []).append(key)
    targets = dict(
        zip(
            by_project,
            await asyncio.gather(
                *(
                    _target(client_for, projects, p, version, action, create_missing)
                    for p in by_project
                )
            ),
            strict=True,
        )
    )

    limiter = asyncio.Semaphore(CONCURRENCY)

    async def one(key: str) -> Outcome:
        if key in invalid:
            return invalid[key]
        target = targets[project_of(key)]
        if target.client is None or target.version is None:
            return Outcome(key, target.result, target.reason)
        async with limiter:
            return await _apply(target.client, key, target.version, action)

    outcomes = await asyncio.gather(*(one(key) for key in order))

    lines = [f"Fix version {version!r}: {action} on {len(order)} issue(s)", ""]
    lines += [o.line() for o in outcomes]
    reports = [
        f"- {project}{' (version created)' if t.created else ''}: "
        f"{report_url(t.client, project, str(t.version['id']))}"
        for project, t in targets.items()
        if t.client is not None and t.version is not None
    ]
    if reports:
        lines += ["", "Release reports:", *reports]
    counts = ", ".join(
        f"{n} {result}"
        for result in RESULTS
        if (n := sum(o.result == result for o in outcomes))
    )
    lines += ["", f"Result: {counts}."]
    return "\n".join(lines)


# -- tool registration --------------------------------------------------------------


def register(mcp: FastMCP, client_for: ClientFor, projects: frozenset[str]) -> None:
    """Adds the version write tools; call after writes.register."""
    errors = (AtlassianError, ConfigError, ValueError, KeyError, ReadOnlyViolationError)

    async def for_project(site: str | None, project: str) -> JiraWriteClient:
        return await client_for(site, issue_key=f"{normalize_project(project)}-0")

    @mcp.tool
    async def create_version(
        project: str,
        name: str,
        description: str | None = None,
        start_date: str | None = None,
        release_date: str | None = None,
        driver: str | None = None,
        site: str | None = None,
    ) -> str:
        """Create a release version in one Jira project. Safe to repeat: if a
        version with exactly that name exists, it is returned unchanged.

        Args:
            project: project key, e.g. "OM".
            name: the exact version name, e.g. "mytrips-3.70.0". Never altered.
            description: plain text.
            start_date: YYYY-MM-DD.
            release_date: the planned release day, YYYY-MM-DD.
            driver: the person responsible for the release: display name or
                email of a user of that project.
            site: Only with several configured sites: the site name. Optional
                here, the project identifies the site.
        """
        try:
            return await create_version_impl(
                await for_project(site, project),
                project,
                name,
                description,
                start_date,
                release_date,
                driver,
            )
        except errors as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def update_version(
        project: str,
        name: str,
        released: bool | None = None,
        release_date: str | None = None,
        start_date: str | None = None,
        description: str | None = None,
        driver: str | None = None,
        site: str | None = None,
    ) -> str:
        """Change one release version, typically to mark it released. Only the
        passed fields change; repeating a call changes nothing. Issues are never
        moved to another version: the count of unresolved ones is reported.
        A version can't be renamed or archived here.

        Args:
            project: project key, e.g. "OM".
            name: the exact name of an existing version.
            released: true marks it released, false unreleased.
            release_date: YYYY-MM-DD. With released=true and no date, a version
                that has no release date yet gets today.
            start_date: YYYY-MM-DD.
            description: plain text; replaces the old one.
            driver: the person responsible for the release: display name or
                email of a user of that project. Can be changed, not removed.
            site: Only with several configured sites: the site name. Optional
                here, the project identifies the site.
        """
        try:
            return await update_version_impl(
                await for_project(site, project),
                project,
                name,
                released,
                release_date,
                start_date,
                description,
                driver,
            )
        except errors as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def set_fix_version(
        issue_keys: list[str],
        version: str,
        action: Action = "add",
        create_missing: bool = False,
        site: str | None = None,
    ) -> str:
        """Add one version to (or remove it from) the Fix versions of many
        issues. Other fix versions of an issue are kept. Safe to repeat.

        A version belongs to one project, so each issue gets the version of
        that name in its own project. Returns one line per issue: added,
        removed, unchanged, skipped or failed (with the reason); one issue
        failing does not stop the others.

        Args:
            issue_keys: up to 100 keys, may span projects, e.g.
                ["OM-3947", "MEINSAPP-878"].
            version: the exact version name, e.g. "mytrips-3.70.0".
            action: "add" or "remove".
            create_missing: with "add", create the version in a project that
                lacks it instead of skipping that project's issues.
            site: Only with several configured sites: the site name. Optional
                here, each issue key's project identifies the site.
        """
        try:
            return await set_fix_version_impl(
                lambda project: for_project(site, project),
                projects,
                issue_keys,
                version,
                action,
                create_missing,
            )
        except errors as exc:
            raise ToolError(str(exc)) from exc

    mcp.instructions = (mcp.instructions or "").replace(
        "unlink_issues). Nothing can be deleted",
        "unlink_issues, and the release tools create_version, update_version, "
        "set_fix_version). Releases: a version belongs to one project, so a "
        "release spanning projects needs the same version name in each; "
        "set_fix_version handles that per issue, and list_versions shows what "
        "exists. Pass version names exactly as given. Nothing can be deleted",
    )
