"""Confluence read operations (shared by the MCP tools and --check).

Uses the v2 REST API (``/wiki/api/v2``) wherever it exists; CQL search and
user lookup only exist in v1 (``/wiki/rest/api``). Every call is a GET.

v2 responses identify people by account ID only, so names are resolved
separately and cached per process; failures degrade to "Unknown user"
instead of failing the whole tool call.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Iterable
from typing import Any, TypeVar

from fastmcp.utilities.types import Image

from .client import AtlassianError, ConfluenceClient
from .confluence_render import (
    CommentThread,
    ContentKind,
    PageRef,
    has_media,
    normalize_attachment_id,
    page_account_ids,
    parse_page_ref,
    render_comment_threads,
    render_page,
    render_page_context,
    render_search_results,
)
from .images import check_image_metadata, image_from_payload

logger = logging.getLogger(__name__)

V2 = "/wiki/api/v2"
BODY_PARAMS = {"body-format": "atlas_doc_format"}
COMMENT_PARAMS = {"body-format": "atlas_doc_format", "sort": "created-date"}

MAX_PAGE_SIZE = 250
MAX_COMMENTS = 100
# Each comment's replies cost one request, so nesting is capped.
MAX_REPLY_DEPTH = 3
MAX_CONCURRENT_REQUESTS = 8

# Looks like CQL rather than plain search words.
_CQL_RE = re.compile(r"!=|=|~|\b(?:AND|OR|NOT|IN|ORDER BY)\b")

T = TypeVar("T")

# Per-process caches; only successful lookups are stored.
_user_names: dict[str, str] = {}
_spaces: dict[str, dict[str, Any]] = {}


async def _gather_limited(awaitables: Iterable[Awaitable[T]]) -> list[T]:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    async def run(awaitable: Awaitable[T]) -> T:
        async with semaphore:
            return await awaitable

    return list(await asyncio.gather(*(run(a) for a in awaitables)))


async def _paginate(
    client: ConfluenceClient,
    path: str,
    params: dict[str, Any],
    limit: int,
) -> list[dict[str, Any]]:
    """Follow v2 cursor links until `limit` results are collected."""
    results: list[dict[str, Any]] = []
    next_path: str | None = path
    query: dict[str, Any] | None = {**params, "limit": min(limit, MAX_PAGE_SIZE)}
    while next_path and len(results) < limit:
        data = await client.get_json(next_path, params=query)
        results.extend(r for r in data.get("results") or [] if isinstance(r, dict))
        next_link = (data.get("_links") or {}).get("next")
        if next_link and not str(next_link).startswith("/wiki"):
            next_link = f"/wiki{next_link}"
        next_path = next_link
        query = None  # the next link already carries the query string
    return results[:limit]


async def fetch_content(
    client: ConfluenceClient, ref: PageRef, with_body: bool
) -> tuple[ContentKind, dict[str, Any]]:
    """Fetch a page or blog post; a bare ID tries pages first, then blog posts."""
    params: dict[str, Any] = {"include-labels": "true"}
    if with_body:
        params.update(BODY_PARAMS)
    kinds: list[ContentKind] = [ref.kind] if ref.kind else ["pages", "blogposts"]
    for kind in kinds:
        try:
            content = await client.get_json(f"{V2}/{kind}/{ref.content_id}", params)
            return kind, content
        except AtlassianError as exc:
            if exc.status_code != 404 or ref.kind is not None:
                raise
    raise AtlassianError(
        f"No Confluence page or blog post with ID {ref.content_id} exists — or "
        "your account lacks permission to see it (Confluence reports both as 404).",
        status_code=404,
    )


async def resolve_users(
    client: ConfluenceClient, account_ids: Iterable[str]
) -> dict[str, str]:
    missing = sorted({i for i in account_ids if i and i not in _user_names})

    async def lookup(account_id: str) -> None:
        try:
            user = await client.get_json(
                "/wiki/rest/api/user", params={"accountId": account_id}
            )
        except AtlassianError as exc:
            logger.warning("Could not resolve Confluence user %s: %s", account_id, exc)
            return
        name = user.get("displayName") or user.get("publicName")
        if name:
            _user_names[account_id] = str(name)

    await _gather_limited(lookup(account_id) for account_id in missing)
    return dict(_user_names)


async def get_space(client: ConfluenceClient, space_id: Any) -> dict[str, Any] | None:
    if not space_id:
        return None
    key = str(space_id)
    if key not in _spaces:
        try:
            _spaces[key] = await client.get_json(f"{V2}/spaces/{key}")
        except AtlassianError as exc:
            logger.warning("Could not fetch Confluence space %s: %s", key, exc)
            return None
    return _spaces[key]


async def fetch_attachments(
    client: ConfluenceClient, kind: ContentKind, content_id: str
) -> list[dict[str, Any]]:
    return await _paginate(
        client, f"{V2}/{kind}/{content_id}/attachments", {}, MAX_PAGE_SIZE
    )


async def _with_replies(
    client: ConfluenceClient,
    comment: dict[str, Any],
    inline: bool,
    depth: int,
) -> CommentThread:
    thread = CommentThread(comment, inline)
    if depth >= MAX_REPLY_DEPTH or not comment.get("id"):
        return thread
    collection = "inline-comments" if inline else "footer-comments"
    children = await _paginate(
        client,
        f"{V2}/{collection}/{comment['id']}/children",
        COMMENT_PARAMS,
        MAX_COMMENTS,
    )
    thread.replies = await _gather_limited(
        _with_replies(client, child, inline, depth + 1) for child in children
    )
    return thread


async def fetch_comment_threads(
    client: ConfluenceClient, kind: ContentKind, content_id: str, limit: int
) -> list[CommentThread]:
    """Footer comments first, then inline comments, each with nested replies."""
    limit = max(1, min(limit, MAX_COMMENTS))
    footer, inline = await asyncio.gather(
        _paginate(
            client, f"{V2}/{kind}/{content_id}/footer-comments", COMMENT_PARAMS, limit
        ),
        _paginate(
            client, f"{V2}/{kind}/{content_id}/inline-comments", COMMENT_PARAMS, limit
        ),
    )
    roots = [(c, False) for c in footer] + [(c, True) for c in inline]
    threads = await _gather_limited(
        _with_replies(client, comment, is_inline, depth=0)
        for comment, is_inline in roots
    )
    with_images = [t for root in threads for t in root.walk() if has_media(t.comment)]
    await _gather_limited(_label_comment_media(client, t) for t in with_images)
    return threads


async def _label_comment_media(client: ConfluenceClient, thread: CommentThread) -> None:
    """Comment attachments only exist in the v1 API; without this lookup
    a pasted screenshot has no attachment ID and cannot be fetched."""
    comment_id = thread.comment.get("id")
    try:
        data = await client.get_json(
            f"/wiki/rest/api/content/{comment_id}/child/attachment",
            params={"limit": 100},
        )
    except AtlassianError as exc:
        logger.warning("Attachments of comment %s unavailable: %s", comment_id, exc)
        return
    thread.media_labels = {
        str(file_id): f"{att.get('title', '?')}, id {att.get('id', '?')}"
        for att in data.get("results") or []
        if isinstance(att, dict)
        and (file_id := (att.get("extensions") or {}).get("fileId"))
    }


def _thread_account_ids(threads: list[CommentThread]) -> set[str]:
    ids: set[str] = set()
    for thread in threads:
        ids |= thread.account_ids()
    return ids


# -- tool implementations ----------------------------------------------------------


async def get_page_impl(
    client: ConfluenceClient, page: str | int, include_comments: bool
) -> str:
    kind, content = await fetch_content(client, parse_page_ref(page), with_body=True)
    content_id = str(content.get("id"))
    attachments, space, comments = await asyncio.gather(
        fetch_attachments(client, kind, content_id),
        get_space(client, content.get("spaceId")),
        fetch_comment_threads(client, kind, content_id, MAX_COMMENTS)
        if include_comments
        else asyncio.sleep(0, result=None),
    )
    account_ids = page_account_ids(content) | _thread_account_ids(comments or [])
    users = await resolve_users(client, account_ids)
    return render_page(
        content, kind, space, attachments, comments, users, client.site_url
    )


async def get_comments_impl(
    client: ConfluenceClient, page: str | int, limit: int
) -> str:
    kind, content = await fetch_content(client, parse_page_ref(page), with_body=False)
    threads = await fetch_comment_threads(client, kind, str(content.get("id")), limit)
    users = await resolve_users(client, _thread_account_ids(threads))
    total = sum(thread.count() for thread in threads)
    header = f"# Comments on **{content.get('title', '?')}** ({total})"
    return f"{header}\n\n{render_comment_threads(threads, users)}"


async def _fetch_tree_item(
    client: ConfluenceClient, item: dict[str, Any]
) -> dict[str, Any]:
    """Ancestors arrive as bare {id, type}; fetch each for its title."""
    item_type = str(item.get("type") or "page")
    try:
        detail = await client.get_json(f"{V2}/{item_type}s/{item.get('id')}")
    except AtlassianError as exc:
        logger.warning("Could not fetch ancestor %s: %s", item.get("id"), exc)
        return item
    return {**item, "title": detail.get("title")}


async def get_page_context_impl(client: ConfluenceClient, page: str | int) -> str:
    kind, content = await fetch_content(client, parse_page_ref(page), with_body=False)
    space = await get_space(client, content.get("spaceId"))
    ancestors: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    if kind == "pages":
        content_id = str(content.get("id"))
        raw_ancestors, children = await asyncio.gather(
            _paginate(client, f"{V2}/pages/{content_id}/ancestors", {}, 50),
            _paginate(
                client, f"{V2}/pages/{content_id}/direct-children", {}, MAX_PAGE_SIZE
            ),
        )
        ancestors = await _gather_limited(
            _fetch_tree_item(client, a) for a in raw_ancestors
        )
    return render_page_context(
        content, kind, space, ancestors, children, client.site_url
    )


def to_cql(query: str) -> str:
    """Plain words become a full-text search over pages and blog posts."""
    text = query.strip()
    if not text:
        raise ValueError("The search query is empty.")
    if _CQL_RE.search(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'type in (page, blogpost) AND text ~ "{escaped}"'


async def search_impl(client: ConfluenceClient, query: str, limit: int) -> str:
    cql = to_cql(query)
    data = await client.get_json(
        "/wiki/rest/api/search",
        params={"cql": cql, "limit": max(1, min(limit, 100))},
    )
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    total = data.get("totalSize")
    return render_search_results(
        cql, results, int(total) if isinstance(total, int) else None
    )


async def get_attachment_impl(
    client: ConfluenceClient, attachment_id: str | int
) -> Image:
    att_id = normalize_attachment_id(attachment_id)
    # Metadata first: unsupported or oversized files are rejected before download.
    meta = await client.get_json(f"{V2}/attachments/{att_id}")
    filename = str(meta.get("title") or att_id)
    image_format = check_image_metadata(
        filename, str(meta.get("mediaType") or ""), int(meta.get("fileSize") or 0)
    )

    container_id = meta.get("pageId") or meta.get("blogPostId")
    candidates: list[str] = []
    if container_id:
        candidates.append(
            f"/wiki/rest/api/content/{container_id}/child/attachment/{att_id}/download"
        )
    if meta.get("downloadLink"):
        candidates.append(f"/wiki{meta['downloadLink']}")
    if not candidates:
        raise ValueError(f"Attachment '{filename}' has no download location.")

    last_error: AtlassianError | None = None
    for path in candidates:
        try:
            data, _ = await client.get_bytes(path)
            return image_from_payload(filename, data, image_format)
        except AtlassianError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error
