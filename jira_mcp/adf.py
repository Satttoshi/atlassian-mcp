"""Atlassian Document Format (ADF) → Markdown.

Jira returns descriptions and comments as ADF: a JSON tree of typed nodes,
not text. This module converts that tree to compact Markdown.

Design rule: unknown node types must degrade gracefully. Anything we don't
recognise is rendered by recursing into its children, so new Atlassian node
types never throw and never silently drop content.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

# Node types that produce block-level output (separated by blank lines).
_BLOCK_SEPARATOR = "\n\n"


def adf_to_markdown(adf: Any) -> str:
    """Convert an ADF document (or fragment) to Markdown.

    Accepts the raw JSON value Jira returns for a description/comment body.
    Returns "" for None; plain strings pass through (Jira API v2 style).
    """
    if adf is None:
        return ""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return str(adf)
    return _render_blocks(adf.get("content") or []).strip()


def _render_blocks(nodes: list[Any], indent: str = "") -> str:
    parts = [_render_node(node, indent) for node in nodes]
    return _BLOCK_SEPARATOR.join(part for part in parts if part.strip())


def _render_inline(nodes: list[Any]) -> str:
    return "".join(_render_node(node) for node in nodes)


def _children(node: dict[str, Any]) -> list[Any]:
    content = node.get("content")
    return content if isinstance(content, list) else []


def _render_node(node: Any, indent: str = "") -> str:
    if not isinstance(node, dict):
        return ""
    node_type = node.get("type", "")
    attrs = node.get("attrs") or {}

    if node_type == "text":
        return _apply_marks(node.get("text", ""), node.get("marks") or [])
    if node_type == "paragraph":
        return _render_inline(_children(node))
    if node_type == "heading":
        level = int(attrs.get("level", 1))
        return f"{'#' * max(1, min(level, 6))} {_render_inline(_children(node))}"
    if node_type == "bulletList":
        return _render_list(node, indent, ordered=False)
    if node_type == "orderedList":
        return _render_list(node, indent, ordered=True)
    if node_type == "codeBlock":
        language = attrs.get("language") or ""
        code = _render_inline(_children(node))
        return f"```{language}\n{code}\n```"
    if node_type == "blockquote":
        inner = _render_blocks(_children(node))
        return "\n".join(f"> {line}" for line in inner.splitlines())
    if node_type == "panel":
        # Panels (info/warning/...) have no Markdown equivalent; render as a
        # labelled blockquote so the intent survives.
        inner = _render_blocks(_children(node))
        label = str(attrs.get("panelType", "panel")).capitalize()
        quoted = "\n".join(f"> {line}" for line in inner.splitlines())
        return f"> **{label}:**\n{quoted}"
    if node_type == "rule":
        return "---"
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        text = str(attrs.get("text", "@unknown"))
        return text if text.startswith("@") else f"@{text}"
    if node_type == "emoji":
        return str(attrs.get("text") or attrs.get("shortName") or "")
    if node_type in ("inlineCard", "blockCard", "embedCard"):
        url = attrs.get("url", "")
        return f"<{url}>" if url else _render_inline(_children(node))
    if node_type == "table":
        return _render_table(node)
    if node_type in ("media", "mediaSingle", "mediaGroup", "mediaInline"):
        return _render_media(node, attrs)
    if node_type == "date":
        return _render_date(attrs)
    if node_type == "status":
        return f"[{attrs.get('text', '')}]"
    if node_type in ("expand", "nestedExpand"):
        title = attrs.get("title") or "Details"
        return f"**{title}**{_BLOCK_SEPARATOR}{_render_blocks(_children(node))}"
    if node_type == "taskList":
        return _render_task_list(node, indent)
    if node_type == "decisionList":
        return "\n".join(
            f"{indent}- **Decision:** {_render_inline(_children(item))}"
            for item in _children(node)
            if isinstance(item, dict)
        )
    if node_type == "caption":
        return f"*{_render_inline(_children(node))}*"
    if node_type in ("extension", "inlineExtension"):
        return _render_extension(attrs)

    # Unknown node type: recurse into children rather than dropping content.
    return _render_blocks(_children(node), indent)


def _apply_marks(text: str, marks: list[Any]) -> str:
    for mark in marks:
        if not isinstance(mark, dict):
            continue
        mark_type = mark.get("type")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "strike":
            text = f"~~{text}~~"
        elif mark_type == "link":
            href = (mark.get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})" if href else text
        # underline / textColor / subsup have no Markdown form; keep plain text.
    return text


def _render_list(node: dict[str, Any], indent: str, ordered: bool) -> str:
    attrs = node.get("attrs") or {}
    number = int(attrs.get("order", 1))
    lines: list[str] = []
    for item in _children(node):
        marker = f"{number}." if ordered else "-"
        number += 1
        body = _render_list_item(item, indent + "  ")
        lines.append(f"{indent}{marker} {body}")
    return "\n".join(lines)


def _render_list_item(item: Any, indent: str) -> str:
    """A listItem may hold a paragraph plus nested lists/blocks."""
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    for child in _children(item):
        rendered = _render_node(child, indent)
        if not rendered.strip():
            continue
        child_type = child.get("type") if isinstance(child, dict) else ""
        if child_type in ("bulletList", "orderedList"):
            parts.append(f"\n{rendered}")  # nested list: own (indented) lines
        elif parts:
            # Extra block inside the same item: indent it under the marker.
            indented = "\n".join(f"{indent}{line}" for line in rendered.splitlines())
            parts.append(f"\n{indented}")
        else:
            parts.append(rendered)
    return "".join(parts)


def _render_table(node: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    header_row = False
    for i, row in enumerate(_children(node)):
        if not isinstance(row, dict):
            continue
        cells = _children(row)
        if i == 0:
            header_row = all(
                isinstance(c, dict) and c.get("type") == "tableHeader" for c in cells
            )
        # Cell content is block-level; flatten to one line for Markdown tables.
        rows.append(
            [
                _render_blocks(_children(c)).replace("\n", " ")
                for c in cells
                if isinstance(c, dict)
            ]
        )
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    lines: list[str] = []
    if header_row:
        header, *body = rows
    else:
        header, body = [""] * width, rows  # Markdown requires a header row
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * width)
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_media(node: dict[str, Any], attrs: dict[str, Any]) -> str:
    if node.get("type") in ("media", "mediaInline"):
        if attrs.get("type") == "external" and attrs.get("url"):
            return f"[image: {attrs['url']}]"
        name = attrs.get("alt") or attrs.get("id") or "media"
        return f"[attachment: {name}]"
    return _render_blocks(_children(node))


def _render_task_list(node: dict[str, Any], indent: str) -> str:
    lines: list[str] = []
    for item in _children(node):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "taskList":
            lines.append(_render_task_list(item, indent + "  "))
            continue
        done = (item.get("attrs") or {}).get("state") == "DONE"
        text = _render_inline(_children(item))
        lines.append(f"{indent}- [{'x' if done else ' '}] {text}")
    return "\n".join(lines)


def _macro_param(attrs: dict[str, Any], name: str) -> str:
    params = (attrs.get("parameters") or {}).get("macroParams") or {}
    value = (params.get(name) or {}).get("value")
    return str(value) if value else ""


def _render_extension(attrs: dict[str, Any]) -> str:
    """Body-less macros (Confluence) render dynamically in the browser, so
    only their identity can be shown; Jira macros keep their issue key/JQL
    so the referenced tickets can be fetched."""
    key = str(attrs.get("extensionKey") or "macro")
    if key == "jira":
        issue_key = _macro_param(attrs, "key")
        if issue_key:
            return f"[Jira issue: {issue_key}]"
        jql = _macro_param(attrs, "jqlQuery")
        if jql:
            return f"[Jira issues: {jql}]"
    metadata = (attrs.get("parameters") or {}).get("macroMetadata") or {}
    title = metadata.get("title") or key
    return f"[macro: {title}]"


def _render_date(attrs: dict[str, Any]) -> str:
    from datetime import datetime

    try:
        # ADF stores dates as epoch milliseconds in a string.
        ts = int(attrs.get("timestamp", "")) / 1000
        return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return str(attrs.get("timestamp", ""))
