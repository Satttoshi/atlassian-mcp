"""Opt-in Jira write tools.

Registered only when JIRA_WRITE_PROJECTS is set; JIRA_WRITE_API_TOKEN is optional.
Text goes through REST v2, so descriptions and comments are Jira wiki markup,
not Markdown or ADF.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from .client import AtlassianError, JiraWriteClient, ReadOnlyViolationError
from .config import ConfigError
from .render import normalize_issue_key

WIKI_MARKUP_HINT = (
    "Text is Jira wiki markup, not Markdown: h3. Heading, *bold*, _italic_, "
    "- or * for bullets, # for numbered lists, {code}...{code}, [label|url], "
    "{{monospace}}."
)

UNASSIGN = {"", "none", "unassigned"}


class ClientFor(Protocol):
    """Returns the write client for the site an issue key or link ID is on."""

    async def __call__(
        self,
        site: str | None,
        *,
        issue_key: str | None = None,
        link_id: str | int | None = None,
    ) -> JiraWriteClient: ...


def _url(client: JiraWriteClient, key: str) -> str:
    return f"{client.site_url}/browse/{key}"


# -- field and user resolution ----------------------------------------------------


async def resolve_fields(
    client: JiraWriteClient, fields: dict[str, Any]
) -> dict[str, Any]:
    """Map display names ("Acceptance Criteria") or IDs to field IDs."""
    if not fields:
        return {}
    catalog = await client.get_json("/rest/api/3/field")
    by_id = {str(f["id"]): str(f["id"]) for f in catalog if f.get("id")}
    by_name: dict[str, list[str]] = {}
    for f in catalog:
        if f.get("id") and f.get("name"):
            by_name.setdefault(str(f["name"]).lower(), []).append(str(f["id"]))
    resolved: dict[str, Any] = {}
    for name, value in fields.items():
        if name in by_id:
            resolved[name] = value
            continue
        ids = by_name.get(name.lower(), [])
        if len(ids) != 1:
            problem = "is ambiguous" if ids else "doesn't exist"
            raise ValueError(
                f"Field {name!r} {problem}. Use the field ID (customfield_…) instead."
                + (f" Candidates: {', '.join(ids)}" if ids else "")
            )
        resolved[ids[0]] = value
    return resolved


async def resolve_account_id(
    client: JiraWriteClient, query: str, *, issue_key: str | None, project: str | None
) -> str | None:
    """None means unassign. Matches display name or email; must be unique."""
    if query.strip().lower() in UNASSIGN:
        return None
    params: dict[str, Any] = {"query": query.strip(), "maxResults": 20}
    if issue_key:
        params["issueKey"] = issue_key
    else:
        params["project"] = project
    users = await client.get_json("/rest/api/3/user/assignable/search", params=params)
    wanted = query.strip().lower()
    exact = [
        u
        for u in users
        if wanted
        in (
            str(u.get("displayName", "")).lower(),
            str(u.get("emailAddress", "")).lower(),
        )
    ]
    matches = exact or users
    if len(matches) != 1:
        names = ", ".join(str(u.get("displayName", "?")) for u in users) or "nobody"
        raise ValueError(
            f"Assignee {query!r} matches {len(matches)} assignable users ({names}). "
            "Use the full display name."
        )
    return str(matches[0]["accountId"])


# -- implementations ----------------------------------------------------------------


async def update_issue_impl(
    client: JiraWriteClient,
    issue_key: str,
    summary: str | None = None,
    description: str | None = None,
    assignee: str | None = None,
    labels: list[str] | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    key = normalize_issue_key(issue_key)
    client.require_project(key)
    body: dict[str, Any] = await resolve_fields(client, fields or {})
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if labels is not None:
        body["labels"] = labels
    if assignee is not None:
        account = await resolve_account_id(
            client, assignee, issue_key=key, project=None
        )
        body["assignee"] = {"accountId": account} if account else None
    if not body:
        raise ValueError("Nothing to update: pass at least one field.")
    await client.send("PUT", f"/rest/api/2/issue/{key}", {"fields": body})
    return f"Updated {key}: {', '.join(sorted(body))}. {_url(client, key)}"


async def create_issue_impl(
    client: JiraWriteClient,
    project: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    parent: str | None = None,
    assignee: str | None = None,
    fields: dict[str, Any] | None = None,
) -> str:
    project = project.strip().upper()
    client.require_project(f"{project}-0")
    body: dict[str, Any] = await resolve_fields(client, fields or {})
    body.update(
        {
            "project": {"key": project},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
    )
    if description:
        body["description"] = description
    if parent:
        body["parent"] = {"key": normalize_issue_key(parent)}
    if assignee:
        account = await resolve_account_id(
            client, assignee, issue_key=None, project=project
        )
        if account:
            body["assignee"] = {"accountId": account}
    created = await client.send("POST", "/rest/api/2/issue", {"fields": body})
    key = str(created["key"])
    return f"Created {key}: {summary}. {_url(client, key)}"


async def add_comment_impl(client: JiraWriteClient, issue_key: str, body: str) -> str:
    key = normalize_issue_key(issue_key)
    client.require_project(key)
    created = await client.send(
        "POST", f"/rest/api/2/issue/{key}/comment", {"body": body}
    )
    return f"Added comment {created['id']} to {key}. {_url(client, key)}"


async def edit_comment_impl(
    client: JiraWriteClient, issue_key: str, comment_id: str | int, body: str
) -> str:
    key = normalize_issue_key(issue_key)
    client.require_project(key)
    await client.send(
        "PUT", f"/rest/api/2/issue/{key}/comment/{int(comment_id)}", {"body": body}
    )
    return f"Edited comment {comment_id} on {key}. {_url(client, key)}"


async def transition_issue_impl(
    client: JiraWriteClient, issue_key: str, status: str
) -> str:
    key = normalize_issue_key(issue_key)
    client.require_project(key)
    data = await client.get_json(f"/rest/api/2/issue/{key}/transitions")
    transitions = data.get("transitions") or []
    wanted = status.strip().lower()
    match = next(
        (
            t
            for t in transitions
            if wanted
            in (str(t.get("name", "")).lower(), str(t["to"].get("name", "")).lower())
        ),
        None,
    )
    if match is None:
        options = ", ".join(f"{t['to'].get('name')}" for t in transitions) or "none"
        raise ValueError(f"{key} can't move to {status!r}. Possible: {options}.")
    await client.send(
        "POST",
        f"/rest/api/2/issue/{key}/transitions",
        {"transition": {"id": match["id"]}},
    )
    return f"Moved {key} to {match['to'].get('name')}. {_url(client, key)}"


async def _link_types(client: JiraWriteClient) -> list[dict[str, Any]]:
    data = await client.get_json("/rest/api/2/issueLinkType")
    return list(data.get("issueLinkTypes") or [])


def _describe(link: dict[str, Any], from_key: str) -> tuple[str, str] | None:
    """How Jira shows a link on from_key: (relation, other key)."""
    link_type = link.get("type") or {}
    if "outwardIssue" in link:
        return str(link_type.get("outward")), str(link["outwardIssue"]["key"])
    if "inwardIssue" in link:
        return str(link_type.get("inward")), str(link["inwardIssue"]["key"])
    return None


async def link_issues_impl(
    client: JiraWriteClient, issue_key: str, relation: str, other_key: str
) -> str:
    key, other = normalize_issue_key(issue_key), normalize_issue_key(other_key)
    wanted = relation.strip().lower()
    link_type = next(
        (
            t
            for t in await _link_types(client)
            if wanted
            in (str(t.get("outward", "")).lower(), str(t.get("inward", "")).lower())
        ),
        None,
    )
    if link_type is None:
        options = "; ".join(
            f"{t.get('outward')} / {t.get('inward')}" for t in await _link_types(client)
        )
        raise ValueError(f"Unknown relation {relation!r}. Possible: {options}.")
    # Jira shows the inwardIssue side with the outward phrase ("A blocks B").
    if wanted == str(link_type.get("outward", "")).lower():
        inward, outward = key, other
    else:
        inward, outward = other, key
    await client.send(
        "POST",
        "/rest/api/2/issueLink",
        {
            "type": {"name": link_type["name"]},
            "inwardIssue": {"key": inward},
            "outwardIssue": {"key": outward},
        },
    )
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": "issuelinks"}
    )
    for link in issue.get("fields", {}).get("issuelinks") or []:
        described = _describe(link, key)
        if described and described[1] == other and described[0].lower() == wanted:
            return f"Linked: {key} {described[0]} {other} (link {link.get('id')})."
    shown = [
        f"{d[0]} {d[1]} (link {link.get('id')})"
        for link in issue.get("fields", {}).get("issuelinks") or []
        if (d := _describe(link, key)) and d[1] == other
    ]
    raise AtlassianError(
        f"Link created, but Jira shows it on {key} as: {'; '.join(shown) or 'nothing'}. "
        "If the direction is wrong, remove it with unlink_issues and report it."
    )


async def unlink_issues_impl(client: JiraWriteClient, link_id: str | int) -> str:
    path = f"/rest/api/2/issueLink/{int(link_id)}"
    link = await client.get_json(path)
    inward = str(link["inwardIssue"]["key"])
    outward = str(link["outwardIssue"]["key"])
    client.require_project(inward)
    client.require_project(outward)
    await client.send("DELETE", path)
    phrase = (link.get("type") or {}).get("outward", "relates to")
    return f"Removed link {link_id}: {inward} {phrase} {outward}."


# -- tool registration --------------------------------------------------------------


def register(mcp: FastMCP, client_for: ClientFor, projects: frozenset[str]) -> None:
    project_list = ", ".join(sorted(projects))

    async def run(
        client: Awaitable[JiraWriteClient],
        impl: Callable[[JiraWriteClient], Awaitable[str]],
    ) -> str:
        try:
            return str(await impl(await client))
        except (
            AtlassianError,
            ConfigError,
            ValueError,
            KeyError,
            ReadOnlyViolationError,
        ) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool
    async def update_issue(
        issue_key: str,
        summary: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        labels: list[str] | None = None,
        fields: dict[str, Any] | None = None,
        site: str | None = None,
    ) -> str:
        """Change fields of an existing Jira issue. Only the passed fields change.

        Args:
            issue_key: e.g. "AC-69".
            summary: new title.
            description: the full new description (replaces the old one).
            assignee: display name or email of an assignable user; "none" unassigns.
            labels: the full new label list (replaces the old one).
            fields: other fields by display name or ID, e.g.
                {"Acceptance Criteria": "...", "Epic Link": "AC-38",
                "Story Points": 3}. Rich-text fields take wiki markup.
            site: Only with several configured sites: the site name. Optional
                here, the issue key's project identifies the site.

        Read the issue with get_issue first; never overwrite text you haven't read.
        """
        return await run(
            client_for(site, issue_key=issue_key),
            lambda c: update_issue_impl(
                c, issue_key, summary, description, assignee, labels, fields
            ),
        )

    @mcp.tool
    async def create_issue(
        project: str,
        summary: str,
        description: str = "",
        issue_type: str = "Task",
        parent: str | None = None,
        assignee: str | None = None,
        fields: dict[str, Any] | None = None,
        site: str | None = None,
    ) -> str:
        """Create a Jira issue and return its key.

        Args:
            project: project key, e.g. "AC".
            summary: the title.
            description: the description.
            issue_type: e.g. "Task", "Story", "Bug", "Epic".
            parent: epic (or parent) key, e.g. "AC-38". If Jira rejects it,
                pass {"Epic Link": "AC-38"} in fields instead.
            assignee: display name or email of an assignable user.
            fields: other fields by display name or ID.
            site: Only with several configured sites: the site name. Optional
                here, the project identifies the site.
        """
        return await run(
            client_for(site, issue_key=f"{project.strip().upper()}-0"),
            lambda c: create_issue_impl(
                c, project, summary, description, issue_type, parent, assignee, fields
            ),
        )

    @mcp.tool
    async def add_comment(issue_key: str, body: str, site: str | None = None) -> str:
        """Add a comment to a Jira issue.

        Args:
            issue_key: e.g. "AC-69".
            body: the comment text.
            site: Only with several configured sites: the site name. Optional
                here, the issue key's project identifies the site.
        """
        return await run(
            client_for(site, issue_key=issue_key),
            lambda c: add_comment_impl(c, issue_key, body),
        )

    @mcp.tool
    async def edit_comment(
        issue_key: str, comment_id: str | int, body: str, site: str | None = None
    ) -> str:
        """Replace the text of an existing comment.

        get_issue and get_comments show each comment's ID as "(comment 12345)".
        Pass the full new text; it replaces the old one.

        Args:
            issue_key: the issue the comment is on, e.g. "AC-69".
            comment_id: e.g. 12345.
            body: the full new text.
            site: Only with several configured sites: the site name. Optional
                here, the issue key's project identifies the site.
        """
        return await run(
            client_for(site, issue_key=issue_key),
            lambda c: edit_comment_impl(c, issue_key, comment_id, body),
        )

    @mcp.tool
    async def transition_issue(
        issue_key: str, status: str, site: str | None = None
    ) -> str:
        """Move an issue to another status, e.g. "DONE", "In Progress".

        Matches the transition name or the target status. On no match, the
        error lists the statuses the issue can move to.

        Args:
            issue_key: e.g. "AC-69".
            status: the transition or target status name.
            site: Only with several configured sites: the site name. Optional
                here, the issue key's project identifies the site.
        """
        return await run(
            client_for(site, issue_key=issue_key),
            lambda c: transition_issue_impl(c, issue_key, status),
        )

    @mcp.tool
    async def link_issues(
        issue_key: str, relation: str, other_key: str, site: str | None = None
    ) -> str:
        """Link two issues so that "<issue_key> <relation> <other_key>" is true.

        Args:
            issue_key: e.g. "AC-66".
            relation: the phrase as Jira shows it, e.g. "is blocked by",
                "blocks", "relates to", "duplicates", "clones".
            other_key: e.g. "AC-14". Must be on the same site as issue_key.
            site: Only with several configured sites: the site name. Optional
                here, issue_key's project identifies the site.

        Example: link_issues("AC-66", "is blocked by", "AC-14"). After
        creating, the link is read back and its direction checked.
        """
        return await run(
            client_for(site, issue_key=issue_key),
            lambda c: link_issues_impl(c, issue_key, relation, other_key),
        )

    @mcp.tool
    async def unlink_issues(link_id: str | int, site: str | None = None) -> str:
        """Remove one issue link. get_issue_context shows link IDs as "(link 12345)".

        Args:
            link_id: e.g. 12345.
            site: Only with several configured sites: the site name shown at
                the top of the get_issue_context result the ID came from.
                Required then, since a link ID does not identify a site.
        """
        return await run(
            client_for(site, link_id=link_id),
            lambda c: unlink_issues_impl(c, link_id),
        )

    mcp.instructions = (mcp.instructions or "").replace(
        "Constraints: strictly read-only; nothing can be created, edited, "
        "commented on, or transitioned.",
        f"Writes: enabled for Jira projects {project_list} only (update_issue, "
        "create_issue, add_comment, edit_comment, transition_issue, link_issues, "
        "unlink_issues). Nothing can be deleted except an issue link. Confluence "
        f"stays read-only. {WIKI_MARKUP_HINT} Read an issue before changing it, "
        "and only write what the user asked for.",
    )
