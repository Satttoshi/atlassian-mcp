"""Render Confluence API payloads as token-efficient Markdown.

Page and comment bodies are requested as ADF (``atlas_doc_format``), so the
same converter that handles Jira descriptions renders them. Confluence v2
payloads carry only account IDs for people; callers resolve them and pass a
``users`` mapping (unresolved IDs render as "Unknown user").
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from .adf import adf_to_markdown
from .render import format_size

ContentKind = Literal["pages", "blogposts"]

_PAGE_PATH_RE = re.compile(r"/pages/(?:edit-v2/|edit/)?(\d+)")
_PAGE_ID_PARAM_RE = re.compile(r"[?&]pageId=(\d+)")
_BLOG_PATH_RE = re.compile(r"/blog/(?:\d{4}/\d{2}/\d{2}/)?(\d+)")
_ATTACHMENT_ID_RE = re.compile(r"^(?:att)?(\d+)$")
_HIGHLIGHT_RE = re.compile(r"@@@(?:end)?hl@@@")


@dataclass(frozen=True)
class PageRef:
    content_id: str
    # None: a bare ID, which may be either a page or a blog post.
    kind: ContentKind | None


def parse_page_ref(raw: str | int) -> PageRef:
    """Accept a numeric content ID or a Confluence page/blog post URL."""
    text = str(raw).strip()
    if text.isdigit():
        return PageRef(text, None)
    for pattern, kind in (
        (_PAGE_PATH_RE, "pages"),
        (_PAGE_ID_PARAM_RE, "pages"),
        (_BLOG_PATH_RE, "blogposts"),
    ):
        match = pattern.search(text)
        if match:
            return PageRef(match.group(1), kind)  # type: ignore[arg-type]
    if "/wiki/x/" in text:
        raise ValueError(
            f"'{raw}' is a Confluence short link, which cannot be resolved "
            "offline. Open it in a browser and pass the full page URL or the "
            "numeric page ID instead."
        )
    raise ValueError(
        f"'{raw}' is not a Confluence page reference; expected a numeric page "
        "ID (e.g. '123456') or a page URL like "
        "https://site.atlassian.net/wiki/spaces/KEY/pages/123456/Title."
    )


def normalize_attachment_id(raw: str | int) -> str:
    """Confluence attachment IDs look like 'att123456'; bare digits get the prefix."""
    match = _ATTACHMENT_ID_RE.match(str(raw).strip().lower())
    if not match:
        raise ValueError(
            f"'{raw}' is not a valid Confluence attachment ID; expected the ID "
            "shown in the Attachments section of get_confluence_page "
            "(e.g. 'att123456'). NOT the page ID and NOT the filename."
        )
    return f"att{match.group(1)}"


@dataclass
class CommentThread:
    comment: dict[str, Any]
    inline: bool
    replies: list[CommentThread] = field(default_factory=list)
    # Images pasted into a comment are attached to the comment, not the page.
    media_labels: dict[str, str] = field(default_factory=dict)

    def walk(self) -> Iterator[CommentThread]:
        yield self
        for reply in self.replies:
            yield from reply.walk()

    def count(self) -> int:
        return 1 + sum(reply.count() for reply in self.replies)

    def account_ids(self) -> set[str]:
        ids = {str(_version(self.comment).get("authorId") or "")}
        for reply in self.replies:
            ids |= reply.account_ids()
        return ids - {""}


def _version(obj: dict[str, Any]) -> dict[str, Any]:
    version = obj.get("version")
    return version if isinstance(version, dict) else {}


def _user(users: dict[str, str], account_id: Any) -> str:
    if not account_id:
        return ""
    return users.get(str(account_id), "Unknown user")


def body_markdown(
    obj: dict[str, Any], media_labels: dict[str, str] | None = None
) -> str:
    """Extract and render the ADF body of a page, blog post, or comment.

    The v2 API delivers ADF as a JSON *string* inside the body object.
    """
    raw = ((obj.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    if not raw:
        return ""
    try:
        adf = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "*(body could not be parsed)*"
    if media_labels:
        _label_media(adf, media_labels)
    return adf_to_markdown(adf)


def has_media(obj: dict[str, Any]) -> bool:
    raw = ((obj.get("body") or {}).get("atlas_doc_format") or {}).get("value")
    return isinstance(raw, str) and '"media' in raw


def _label_media(node: Any, labels: dict[str, str]) -> None:
    """Replace opaque media file IDs with "filename, id attNNN" so images in
    the body can be matched to fetchable attachments."""
    if isinstance(node, dict):
        attrs = node.get("attrs")
        if node.get("type") == "media" and isinstance(attrs, dict):
            label = labels.get(str(attrs.get("id")))
            if label:
                attrs["alt"] = label
        for child in node.get("content") or []:
            _label_media(child, labels)


def attachment_media_labels(attachments: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(a["fileId"]): f"{a.get('title', '?')}, id {a.get('id', '?')}"
        for a in attachments
        if a.get("fileId")
    }


def web_url(site_url: str, obj: dict[str, Any]) -> str:
    webui = (obj.get("_links") or {}).get("webui")
    return f"{site_url}/wiki{webui}" if webui else ""


def space_label(space: dict[str, Any] | None) -> str:
    if not space:
        return ""
    name, key = space.get("name"), space.get("key")
    if name and key:
        return f"{name} ({key})"
    return str(name or key or "")


def page_account_ids(content: dict[str, Any]) -> set[str]:
    ids = {
        str(content.get("authorId") or ""),
        str(content.get("ownerId") or ""),
        str(_version(content).get("authorId") or ""),
    }
    return ids - {""}


def render_page(
    content: dict[str, Any],
    kind: ContentKind,
    space: dict[str, Any] | None,
    attachments: list[dict[str, Any]],
    comments: list[CommentThread] | None,
    users: dict[str, str],
    site_url: str,
) -> str:
    version = _version(content)
    lines = [f"# {content.get('title', '?')}", ""]
    labels = ((content.get("labels") or {}).get("results")) or []
    status = str(content.get("status") or "")
    edited = ""
    if version.get("authorId"):
        edited = (
            f"{_user(users, version.get('authorId'))}, {version.get('createdAt', '?')}"
        )
    facts = [
        ("Type", "Blog post" if kind == "blogposts" else "Page"),
        ("ID", str(content.get("id") or "")),
        ("Space", space_label(space)),
        ("Status", status if status and status != "current" else ""),
        ("Version", str(version.get("number") or "")),
        ("Created by", _user(users, content.get("authorId"))),
        ("Owner", _user(users, content.get("ownerId"))),
        ("Created", str(content.get("createdAt") or "")),
        ("Last edited", edited),
        ("Labels", ", ".join(str(label.get("name", "")) for label in labels)),
        ("URL", web_url(site_url, content)),
    ]
    lines.extend(f"- **{label}:** {value}" for label, value in facts if value)

    body = body_markdown(content, attachment_media_labels(attachments))
    lines.extend(["", "## Content", "", body or "*(empty page)*"])

    if attachments:
        lines.extend(["", f"## Attachments ({len(attachments)})", ""])
        lines.extend(_attachment_line(a) for a in attachments)
        hint = _attachment_hint(attachments)
        if hint:
            lines.extend(["", hint])

    if comments is not None:
        total = sum(thread.count() for thread in comments)
        lines.extend(["", f"## Comments ({total})", ""])
        lines.append(render_comment_threads(comments, users))

    return "\n".join(lines)


def _attachment_line(attachment: dict[str, Any]) -> str:
    title = str(attachment.get("title") or "?")
    mime_type = str(attachment.get("mediaType") or "unknown type")
    size = format_size(int(attachment.get("fileSize") or 0))
    comment = attachment.get("comment")
    note = f" — {comment}" if comment else ""
    return f"- **{title}** (id {attachment.get('id', '?')}, {mime_type}, {size}){note}"


def _attachment_hint(attachments: list[dict[str, Any]]) -> str:
    image_ids = [
        str(a.get("id"))
        for a in attachments
        if str(a.get("mediaType") or "").startswith("image/") and a.get("id")
    ]
    if not image_ids:
        return ""
    return (
        "*(fetch images for visual analysis with the attachment id, e.g. "
        f'get_confluence_attachment(attachment_id="{image_ids[0]}"))*'
    )


def render_comment_threads(threads: list[CommentThread], users: dict[str, str]) -> str:
    if not threads:
        return "*(no comments)*"
    return "\n\n".join(_render_thread(thread, users, depth=0) for thread in threads)


def _render_thread(thread: CommentThread, users: dict[str, str], depth: int) -> str:
    comment = thread.comment
    version = _version(comment)
    author = _user(users, version.get("authorId")) or "Unknown user"
    tags: list[str] = []
    if thread.inline and depth == 0:
        tags.append("inline")
        resolution = comment.get("resolutionStatus")
        if resolution and resolution != "open":
            tags.append(str(resolution))
    if int(version.get("number") or 1) > 1:
        tags.append("edited")
    suffix = f" ({', '.join(tags)})" if tags else ""
    marker = "↳ " if depth else ""
    heading = "#" * min(3 + depth, 6)
    parts = [f"{heading} {marker}{author} — {version.get('createdAt', '?')}{suffix}"]

    selection = (comment.get("properties") or {}).get("inlineOriginalSelection")
    if thread.inline and selection:
        parts.append(f'> on: "{selection}"')
    parts.append(body_markdown(comment, thread.media_labels) or "*(empty comment)*")
    parts.extend(_render_thread(reply, users, depth + 1) for reply in thread.replies)
    return "\n\n".join(parts)


def _tree_line(item: dict[str, Any]) -> str:
    kind = str(item.get("type") or "page")
    title = item.get("title") or "?"
    return f"**{title}** [{kind}, id {item.get('id', '?')}]"


def render_page_context(
    content: dict[str, Any],
    kind: ContentKind,
    space: dict[str, Any] | None,
    ancestors: list[dict[str, Any]],
    children: list[dict[str, Any]],
    site_url: str,
) -> str:
    kind_label = "blog post" if kind == "blogposts" else "page"
    title = content.get("title", "?")
    lines = [
        f"# Context for **{title}** [{kind_label}, id {content.get('id', '?')}]",
        "",
    ]
    space_text = space_label(space)
    if space_text:
        lines.append(f"- **Space:** {space_text}")
    url = web_url(site_url, content)
    if url:
        lines.append(f"- **URL:** {url}")

    if kind == "blogposts":
        lines.extend(["", "*(blog posts have no page tree: no parent or children)*"])
        return "\n".join(lines)

    lines.extend(["", f"## Ancestors ({len(ancestors)}, top-level first)"])
    lines.extend(
        f"{'  ' * depth}- {_tree_line(a)}" for depth, a in enumerate(ancestors)
    )
    if not ancestors:
        lines.append("*(none — top-level page)*")

    lines.extend(["", f"## Children ({len(children)})"])
    lines.extend(f"- {_tree_line(c)}" for c in children)
    if not children:
        lines.append("*(none)*")
    return "\n".join(lines)


def _clean_excerpt(text: str, limit: int = 240) -> str:
    cleaned = " ".join(html.unescape(_HIGHLIGHT_RE.sub("", text)).split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[: limit - 1]}…"


def render_search_results(
    cql: str, results: list[dict[str, Any]], total: int | None
) -> str:
    lines = [f"# Confluence results for `{cql}` ({len(results)} shown)", ""]
    if not results:
        lines.append("*(no matching content)*")
    for result in results:
        content = result.get("content") or {}
        title = html.unescape(
            _HIGHLIGHT_RE.sub(
                "", str(result.get("title") or content.get("title") or "?")
            )
        )
        meta = [str(content.get("type") or result.get("entityType") or "")]
        container = (result.get("resultGlobalContainer") or {}).get("title")
        if container:
            meta.append(str(container))
        if content.get("id"):
            meta.append(f"id {content['id']}")
        line = f"- **{title}** [{', '.join(m for m in meta if m)}]"
        if result.get("lastModified"):
            line += f" — updated {result['lastModified']}"
        excerpt = _clean_excerpt(str(result.get("excerpt") or ""))
        if excerpt:
            line += f"\n  {excerpt}"
        lines.append(line)
    if total is not None and total > len(results):
        lines.extend(
            ["", f"*({total} matches in total — refine the CQL or raise the limit)*"]
        )
    return "\n".join(lines)


def render_remote_links(links: list[dict[str, Any]]) -> str:
    """Jira remote links, with Confluence pages turned into fetchable IDs."""
    lines = [f"## Remote links ({len(links)})"]
    for link in links:
        obj = link.get("object") or {}
        url = str(obj.get("url") or "")
        title = str(obj.get("title") or url or "?")
        relation = link.get("relationship")
        prefix = f"{relation}: " if relation else ""
        line = f"- {prefix}[{title}]({url})" if url else f"- {prefix}{title}"
        app_type = str((link.get("application") or {}).get("type") or "")
        if "confluence" in app_type or "/wiki/" in url:
            try:
                ref = parse_page_ref(url)
            except ValueError:
                ref = None
            if ref:
                line += f" — Confluence page id {ref.content_id} (get_confluence_page)"
        lines.append(line)
    if not links:
        lines.append("*(none)*")
    return "\n".join(lines)
