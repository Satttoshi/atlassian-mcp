"""The MCP server: tool definitions, --check CLI mode, entry point.

stdio discipline: stdout carries only MCP protocol traffic, so nothing here
may print() in server mode. All diagnostics go to stderr via logging.
(--check mode runs outside MCP entirely and prints freely.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, NoReturn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.utilities.types import Image

from . import confluence, writes
from .attachments import AttachmentMeta, read_attachment
from .client import (
    MYSELF_PATH,
    AtlassianClient,
    AtlassianError,
    ConfluenceClient,
    JiraClient,
    JiraWriteClient,
)
from .config import ENV_FILE, ConfigError, Settings, load_settings
from .confluence_render import render_remote_links
from .render import (
    normalize_issue_key,
    render_comments,
    render_context,
    render_issue,
    render_myself,
    render_search_results,
)

logger = logging.getLogger(__name__)

mcp: FastMCP = FastMCP(
    name="Jira & Confluence (read-only)",
    instructions=(
        "Read-only access to Jira Cloud issues and Confluence Cloud pages.\n"
        "\n"
        "Jira — when to use: whenever a Jira issue key appears in the "
        "conversation. A key is an uppercase project prefix, a hyphen, and a "
        "number (e.g. ABC-123, PLAT-4567); keys also show up inside branch "
        "names, commit messages, and Jira URLs. On seeing one, fetch the "
        "ticket instead of guessing what it contains.\n"
        "\n"
        "Jira workflow: start with get_issue for the full ticket, then "
        "get_issue_context to find parents, subtasks, linked issues, and "
        "linked Confluence pages worth reading. Use search_issues (JQL) when "
        "no key is given. When a ticket lists attachments relevant to the "
        "task (e.g. a screenshot on a UI bug, or a log, JSON payload, or CSV "
        "export), fetch them with get_attachment to read them.\n"
        "\n"
        "Confluence — when to use: whenever a Confluence URL (…/wiki/spaces/"
        "…/pages/<id>/…) or page ID appears, or when specs, designs, "
        "runbooks, or decisions are likely documented in the wiki. Read pages "
        "with get_confluence_page (body, attachments, comments); use "
        "get_confluence_page_context to walk the page tree; search with "
        "search_confluence (plain words or CQL). Pages reference Jira issues "
        "as [Jira issue: KEY] — fetch those with get_issue. Fetch images "
        "and text files attached to a page or comment with "
        "get_confluence_attachment.\n"
        "\n"
        "Call whoami only to debug authentication.\n"
        "\n"
        "Constraints: strictly read-only; nothing can be created, edited, "
        "commented on, or transitioned."
    ),
)

# One client per product per process, created on first use. Lazy so that
# importing this module (e.g. in tests) needs no credentials.
_client: JiraClient | None = None
_confluence_client: ConfluenceClient | None = None


def _get_client() -> JiraClient:
    global _client
    if _client is None:
        _client = JiraClient(load_settings())
    return _client


def _get_confluence_client() -> ConfluenceClient:
    global _confluence_client
    if _confluence_client is None:
        _confluence_client = ConfluenceClient(load_settings())
    return _confluence_client


# *all so custom fields (acceptance criteria etc.) come along; comment and
# worklog are excluded because comments are fetched separately with ordering.
ISSUE_FIELDS = "*all,-comment,-worklog"
CONTEXT_FIELDS = "summary,status,issuetype,parent,subtasks,issuelinks"
SEARCH_FIELDS = ["summary", "status", "issuetype", "priority", "assignee", "updated"]

# -- shared implementations (used by both MCP tools and --check) ---------------


async def _fetch_comments(
    client: JiraClient, key: str, limit: int, newest_first: bool
) -> list[dict[str, Any]]:
    data = await client.get_json(
        f"/rest/api/3/issue/{key}/comment",
        params={
            "maxResults": max(1, min(limit, 100)),
            "orderBy": "-created" if newest_first else "+created",
        },
    )
    comments = data.get("comments") or []
    return list(comments)


# Field-name catalog (customfield_10123 -> "Acceptance Criteria"), fetched
# once per process. Only successful lookups are cached, so a transient
# failure is retried on the next call.
_field_names: dict[str, str] | None = None


async def _get_field_names(client: JiraClient) -> dict[str, str]:
    global _field_names
    if _field_names is None:
        try:
            catalog = await client.get_json("/rest/api/3/field")
        except AtlassianError as exc:
            logger.warning(
                "Field catalog unavailable; custom fields will show raw IDs: %s", exc
            )
            return {}
        _field_names = {
            str(field["id"]): str(field.get("name") or field["id"])
            for field in catalog
            if isinstance(field, dict) and field.get("id")
        }
    return _field_names


async def _get_issue_impl(
    client: JiraClient, issue_key: str, include_comments: bool
) -> str:
    key = normalize_issue_key(issue_key)
    field_names = await _get_field_names(client)
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": ISSUE_FIELDS}
    )
    comments = (
        await _fetch_comments(client, key, 100, False) if include_comments else None
    )
    return render_issue(issue, comments, field_names)


async def _get_issue_context_impl(client: JiraClient, issue_key: str) -> str:
    key = normalize_issue_key(issue_key)
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": CONTEXT_FIELDS}
    )
    fields = issue.get("fields") or {}
    issue_type = fields.get("issuetype") or {}

    # Epics don't list their children on the issue itself; one extra search
    # resolves them. hierarchyLevel 1 == epic-level in Jira's type hierarchy.
    epic_children: list[dict[str, Any]] | None = None
    if (
        issue_type.get("hierarchyLevel") == 1
        or str(issue_type.get("name", "")).lower() == "epic"
    ):
        result = await client.search_jql(
            {
                "jql": f'parent = "{key}" ORDER BY created ASC',
                "maxResults": 50,
                "fields": ["summary", "status", "issuetype"],
            }
        )
        epic_children = list(result.get("issues") or [])

    context = render_context(issue, epic_children)
    # Remote links (Confluence pages, PRs, ...) are a bonus: never fail on them.
    try:
        links = await client.get_json(f"/rest/api/3/issue/{key}/remotelink")
    except AtlassianError as exc:
        logger.warning("Remote links unavailable for %s: %s", key, exc)
        return context
    remote = [link for link in links or [] if isinstance(link, dict)]
    return f"{context}\n\n{render_remote_links(remote)}"


async def _search_issues_impl(client: JiraClient, jql: str, limit: int) -> str:
    result = await client.search_jql(
        {"jql": jql, "maxResults": max(1, min(limit, 100)), "fields": SEARCH_FIELDS}
    )
    issues = list(result.get("issues") or [])
    return render_search_results(
        jql, issues, has_more=bool(result.get("nextPageToken"))
    )


async def _reject_issue_key_as_attachment_id(client: JiraClient, key: str) -> NoReturn:
    """Turn the issue-key-instead-of-ID mistake into a one-step recovery:
    the error itself lists the valid attachment IDs of that issue."""
    issue = await client.get_json(
        f"/rest/api/3/issue/{key}", params={"fields": "attachment"}
    )
    attachments = (issue.get("fields") or {}).get("attachment") or []
    if not attachments:
        raise ValueError(
            f"'{key}' is an issue key, not an attachment ID, and {key} has "
            "no attachments."
        )
    listing = "; ".join(
        f"{a.get('filename', '?')} = id {a.get('id', '?')}" for a in attachments
    )
    raise ValueError(
        f"'{key}' is an issue key, not an attachment ID. Attachments on {key}: "
        f"{listing}. Call get_attachment again with one of these numeric IDs."
    )


async def _get_attachment_impl(
    client: JiraClient, attachment_id: str | int
) -> Image | str:
    att_id = str(attachment_id).strip()
    if not att_id.isdigit():
        try:
            key = normalize_issue_key(att_id)
        except ValueError:
            raise ValueError(
                f"'{attachment_id}' is not a valid attachment ID; expected the "
                "numeric ID shown in the Attachments section of get_issue "
                "(e.g. '10023')."
            ) from None
        await _reject_issue_key_as_attachment_id(client, key)

    meta = await client.get_json(f"/rest/api/3/attachment/{att_id}")
    return await read_attachment(
        AttachmentMeta(
            id=att_id,
            filename=str(meta.get("filename") or att_id),
            mime_type=str(meta.get("mimeType") or ""),
            size=int(meta.get("size") or 0),
        ),
        lambda: _download(client, f"/rest/api/3/attachment/content/{att_id}"),
    )


async def _download(client: AtlassianClient, path: str) -> bytes:
    data, _ = await client.get_bytes(path)
    return data


async def _whoami_impl(client: JiraClient) -> str:
    account = await client.get_json(MYSELF_PATH)
    return render_myself(account, client.base_url or "?", client.used_cloud_id_fallback)


async def _account_section(client: AtlassianClient) -> str:
    """One product's auth report; an error becomes the section's content."""
    name = client.product.name
    try:
        account = await client.get_json(client.product.probe_path)
    except AtlassianError as exc:
        return f"## {name}\n\n**Not connected:** {exc}"
    report = render_myself(
        account, client.base_url or "?", client.used_cloud_id_fallback
    )
    return f"## {name}\n\n{report}"


# -- MCP tools ------------------------------------------------------------------
# Docstrings are the API the model sees; they describe behaviour, not code.


@mcp.tool
async def get_issue(issue_key: str, include_comments: bool = True) -> str:
    """Get full detail for one Jira issue as Markdown.

    Returns summary, status, type, priority, resolution, assignee, reporter,
    labels, components, fix versions, created/updated timestamps, the full
    description, all non-empty custom fields (e.g. acceptance criteria,
    story points) with their display names, the attachment list (fetch
    images via get_attachment), and (by default) the complete comment
    thread.

    Args:
        issue_key: Jira issue key like "PROJ-123" (case and surrounding
            whitespace are tolerated).
        include_comments: Set False to omit the comment thread when only the
            ticket body is needed — the response is much shorter.

    Prefer get_issue_context when the goal is discovering *related* tickets
    rather than reading this one.
    """
    try:
        return await _get_issue_impl(_get_client(), issue_key, include_comments)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_comments(
    issue_key: str, limit: int = 100, newest_first: bool = False
) -> str:
    """Get only the comment thread of a Jira issue, as Markdown.

    Each comment includes author, timestamp, whether it was edited, and any
    visibility restriction. Use this instead of get_issue when the ticket
    body is already known and only the discussion is needed.

    Args:
        issue_key: Jira issue key like "PROJ-123".
        limit: Maximum number of comments to return (1-100).
        newest_first: Set True to get the most recent comments first —
            useful for long threads where only the latest state matters.
    """
    try:
        client = _get_client()
        key = normalize_issue_key(issue_key)
        comments = await _fetch_comments(client, key, limit, newest_first)
        return render_comments(comments)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_issue_context(issue_key: str) -> str:
    """Map the reference graph around a Jira issue: parent, subtasks, links.

    Returns the parent (e.g. epic), all subtasks, and all linked issues —
    each resolved to key + type + status + summary so the next issue to read
    can be chosen without further calls. Link relationships are given in
    human terms with correct direction ("blocks" vs "is blocked by"). For
    epics, also lists the issues inside the epic.

    Args:
        issue_key: Jira issue key like "PROJ-123".

    Use this first when exploring an unfamiliar ticket; then get_issue on
    the keys that look relevant.
    """
    try:
        return await _get_issue_context_impl(_get_client(), issue_key)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def search_issues(jql: str, limit: int = 25) -> str:
    """Search Jira with a JQL query; returns a compact Markdown list.

    Each result line has key, type, status, summary, assignee and last
    update. Example JQL: 'project = PROJ AND status != Done ORDER BY
    updated DESC'. Results are capped at `limit` (1-100); the output notes
    when more matches exist.

    Args:
        jql: A JQL query string (sent via POST, so any length/complexity works).
        limit: Maximum number of issues to return.
    """
    try:
        return await _search_issues_impl(_get_client(), jql, limit)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_attachment(attachment_id: str | int) -> Image | str:
    """Download an image or text file attached to a Jira issue.

    Images are returned as images so they can be looked at directly, e.g. a
    UI bug report where the screenshot shows the actual broken state. Text
    files (logs, stack traces, JSON/HAR, XML, YAML, CSV, source code, ...)
    are returned as their text content. Use it when a ticket's Attachments
    section (from get_issue) lists a file relevant to the task. Files of
    unknown type (e.g. Dockerfile, app.log.1) are checked by content.

    Files up to 5 MB are supported. Text longer than 20,000 characters is
    saved in full to a local file; the result then gives that file's path
    plus its first and last lines, so search it with grep or read it in
    ranges. Videos, PDFs, archives and other binary files are rejected with
    an explanatory error.

    Args:
        attachment_id: The numeric attachment ID exactly as listed in
            get_issue output, e.g. get_attachment(attachment_id="107474").
            NOT the issue key and NOT the filename.
    """
    try:
        return await _get_attachment_impl(_get_client(), attachment_id)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_confluence_page(page: str | int, include_comments: bool = True) -> str:
    """Get a Confluence page or blog post as Markdown.

    Returns title, space, version, author/owner, last edit, labels, URL, the
    full body (tables, macros, task lists, embedded images labelled with
    their attachment IDs), the attachment list (fetch images via
    get_confluence_attachment), and (by default) all footer and inline
    comments with their reply threads. Jira issues embedded in the page
    appear as [Jira issue: KEY]; read them with get_issue.

    Args:
        page: A numeric page ID ("123456") or a Confluence URL, e.g.
            "https://site.atlassian.net/wiki/spaces/ENG/pages/123456/Title".
            Blog post URLs work too.
        include_comments: Set False to omit comments when only the page body
            is needed — the response is much shorter.
    """
    try:
        return await confluence.get_page_impl(
            _get_confluence_client(), page, include_comments
        )
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_confluence_comments(
    page: str | int, limit: int = 100, newest_first: bool = False
) -> str:
    """Get only the comments on a Confluence page or blog post, as Markdown.

    Footer comments come first, then inline comments (each quoting the page
    text it was attached to, and marked if resolved). Replies are nested
    under their parent. Use this instead of get_confluence_page when the
    body is already known and only the discussion is needed.

    Args:
        page: A numeric page ID or a Confluence page URL.
        limit: Maximum number of top-level comments per kind (1-100).
        newest_first: Set True to get the most recent comments first —
            useful for long discussions where only the latest state
            matters. Replies within a thread stay in chronological order.
    """
    try:
        return await confluence.get_comments_impl(
            _get_confluence_client(), page, limit, newest_first
        )
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_confluence_page_context(page: str | int) -> str:
    """Map where a Confluence page sits in its space's page tree.

    Returns the space, the ancestor chain (top-level first) and the direct
    children (pages, folders, whiteboards, ...), each with title and ID so
    the next page to read can be chosen without further calls.

    Args:
        page: A numeric page ID or a Confluence page URL.

    Use this to find parent overviews or sibling specs; then
    get_confluence_page on the IDs that look relevant.
    """
    try:
        return await confluence.get_page_context_impl(_get_confluence_client(), page)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def search_confluence(query: str, limit: int = 25) -> str:
    """Search Confluence; returns a compact Markdown list with excerpts.

    Plain words (e.g. "payment retry design") run a full-text search over
    pages and blog posts. Anything containing CQL syntax is sent as CQL
    as-is, e.g. 'space = ENG AND title ~ "onboarding" ORDER BY
    lastmodified DESC' or 'type = page AND label = "adr"'. Each result has
    title, type, space, page ID, last update and an excerpt.

    Args:
        query: Search words or a CQL query.
        limit: Maximum number of results (1-100).
    """
    try:
        return await confluence.search_impl(_get_confluence_client(), query, limit)
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def get_confluence_attachment(attachment_id: str | int) -> Image | str:
    """Download a file attached to a Confluence page or comment.

    Images (png, jpeg, gif, webp) come back as the image itself, for visual
    analysis — e.g. an architecture diagram, mockup, or screenshot. Text
    files (logs, JSON, YAML, CSV, source code, ...) come back as text; text
    over 20,000 characters is saved to a local file and returned as its
    path plus a head/tail preview. Max 5 MB; videos, PDFs, archives and
    other binary files are rejected.

    Args:
        attachment_id: The attachment ID as listed by get_confluence_page,
            or shown next to an image in a comment, e.g. "[attachment:
            bug.png, id att123456]" -> get_confluence_attachment(
            attachment_id="att123456"). NOT the page ID and NOT the filename.
    """
    try:
        return await confluence.get_attachment_impl(
            _get_confluence_client(), attachment_id
        )
    except (AtlassianError, ConfigError, ValueError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool
async def whoami() -> str:
    """Report which Atlassian account this server is authenticated as.

    Covers Jira and Confluence separately (they may use different tokens):
    display name, email, account ID, and which base URL was selected (site
    URL vs cloud-ID fallback). Call this first when any other tool returns
    authentication or permission errors — it isolates whether the problem
    is the credentials or the specific resource.
    """
    try:
        sections = await asyncio.gather(
            _account_section(_get_client()),
            _account_section(_get_confluence_client()),
        )
    except ConfigError as exc:
        raise ToolError(str(exc)) from exc
    return "\n\n".join(sections)


# -- --check mode ----------------------------------------------------------------


async def _run_check(issue_key: str | None, page: str | None) -> int:
    """Diagnostic mode for terminals and the PyCharm debugger.

    Runs entirely outside MCP, so printing to stdout is safe here.
    """
    print(f".env file: {'found' if ENV_FILE.is_file() else 'NOT found'} at {ENV_FILE}")
    try:
        settings: Settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}")
        return 1
    print(f"Site URL: {settings.site_url}")
    print(f"Cloud ID override: {settings.cloud_id or '(not set — auto-detect)'}")
    separate_token = "yes" if settings.confluence_api_token else "no (shared token)"
    print(f"Separate Confluence token: {separate_token}")

    jira = JiraClient(settings)
    wiki = ConfluenceClient(settings)
    try:
        print(f"\n{await _account_section(jira)}")
        print(f"\n{await _account_section(wiki)}")
        if issue_key:
            print(f"\n--- {issue_key} ---\n")
            print(await _get_issue_impl(jira, issue_key, include_comments=True))
            print("\n--- context ---\n")
            print(await _get_issue_context_impl(jira, issue_key))
        if page:
            print(f"\n--- Confluence {page} ---\n")
            print(await confluence.get_page_impl(wiki, page, include_comments=True))
            print("\n--- page context ---\n")
            print(await confluence.get_page_context_impl(wiki, page))
    except (AtlassianError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    finally:
        await jira.aclose()
        await wiki.aclose()
    print("\nAll requested checks ran (see sections above for per-product status).")
    return 0


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="jira_mcp",
        description=(
            "Read-only Jira & Confluence MCP server (stdio). "
            "Use --check to diagnose outside MCP."
        ),
    )
    parser.add_argument(
        "--check",
        nargs="?",
        const="",
        default=None,
        metavar="ISSUE_KEY",
        help="Run connectivity diagnostics and exit; optionally fetch ISSUE_KEY in full.",
    )
    parser.add_argument(
        "--page",
        default=None,
        metavar="PAGE",
        help="With --check: also fetch this Confluence page ID or URL in full.",
    )
    args = parser.parse_args()

    if args.check is not None or args.page:
        # asyncio.run() creates an event loop, runs one coroutine, and cleans
        # up — the standard way to call async code from a sync entry point.
        sys.exit(asyncio.run(_run_check(args.check or None, args.page)))

    _register_writes()
    mcp.run()  # stdio transport is FastMCP's default


_write_client: JiraWriteClient | None = None


def _get_write_client() -> JiraWriteClient:
    global _write_client
    if _write_client is None:
        _write_client = JiraWriteClient(load_settings())
    return _write_client


def _register_writes() -> None:
    try:
        settings = load_settings()
    except ConfigError:
        return  # the read tools report the config error on first use
    if not settings.writes_enabled:
        if settings.jira_write_api_token:
            logger.warning("Jira writes stay disabled: JIRA_WRITE_PROJECTS is not set.")
        return
    writes.register(mcp, _get_write_client)
    logger.info(
        "Jira writes enabled for projects: %s",
        ", ".join(sorted(settings.jira_write_projects)),
    )


if __name__ == "__main__":
    main()
