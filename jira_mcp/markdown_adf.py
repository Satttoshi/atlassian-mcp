"""Markdown to Atlassian Document Format, for rich text Jira takes only as ADF.

Covers what release notes use: headings, paragraphs, nested bullet and
numbered lists, fenced code, rules, and inline bold, italic, code and links.
Two wiki-markup forms are accepted too, since the other write tools speak
wiki markup and these can't be mistaken for Markdown: "h2. Heading" and
"[label|url]".
"""

from __future__ import annotations

import re
from typing import Any

Node = dict[str, Any]

_HEADING = re.compile(r"(#{1,6})\s+(.*?)\s*#*$")
_WIKI_HEADING = re.compile(r"h([1-6])\.\s+(.*)$")
_LIST_ITEM = re.compile(r"(\s*)([-*+]|\d+[.)])\s+(.*)$")
_RULE = re.compile(r"(-{3,}|\*{3,}|_{3,})$")
_FENCE = re.compile(r"```\s*(\S*)\s*$")

_INLINE = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|\*\*(?P<strong>.+?)\*\*"
    r"|\[(?P<label>[^\]]+)\]\((?P<href>[^)\s]+)\)"
    r"|\[(?P<wiki_label>[^\]|]+)\|(?P<wiki_href>[^\]\s]+)\]"
    r"|(?P<url>https?://[^\s<>\])]+)"
    r"|(?<![\w*])\*(?P<em>[^*\s](?:[^*]*[^*\s])?)\*(?![\w*])"
    r"|(?<!\w)_(?P<em2>[^_\s](?:[^_]*[^_\s])?)_(?!\w)"
)


def markdown_to_adf(text: str) -> Node:
    lines = text.replace("\r\n", "\n").split("\n")
    return {"type": "doc", "version": 1, "content": _blocks(lines)}


def _text(text: str, marks: list[Node]) -> list[Node]:
    if not text:
        return []
    node: Node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return [node]


def _link(href: str) -> Node:
    return {"type": "link", "attrs": {"href": href}}


def _inline(text: str, marks: list[Node] | None = None) -> list[Node]:
    marks = marks or []
    nodes: list[Node] = []
    position = 0
    for match in _INLINE.finditer(text):
        nodes += _text(text[position : match.start()], marks)
        position = match.end()
        if match["code"]:
            nodes += _text(match["code"], [*marks, {"type": "code"}])
        elif match["strong"]:
            nodes += _inline(match["strong"], [*marks, {"type": "strong"}])
        elif match["label"]:
            nodes += _inline(match["label"], [*marks, _link(match["href"])])
        elif match["wiki_label"]:
            nodes += _inline(match["wiki_label"], [*marks, _link(match["wiki_href"])])
        elif match["url"]:
            url = match["url"].rstrip(".,;:!?")
            position -= len(match["url"]) - len(url)
            nodes += _text(url, [*marks, _link(url)])
        else:
            nodes += _inline(match["em"] or match["em2"], [*marks, {"type": "em"}])
    return nodes + _text(text[position:], marks)


def _paragraph(text: str) -> Node:
    return {"type": "paragraph", "content": _inline(text)}


def _is_block_start(line: str) -> bool:
    stripped = line.strip()
    return bool(
        not stripped
        or _HEADING.match(stripped)
        or _WIKI_HEADING.match(stripped)
        or _LIST_ITEM.match(line)
        or _RULE.match(stripped)
        or _FENCE.match(stripped)
    )


def _blocks(lines: list[str]) -> list[Node]:
    blocks: list[Node] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
        elif fence := _FENCE.match(stripped):
            end = next(
                (n for n in range(i + 1, len(lines)) if lines[n].strip() == "```"),
                len(lines),
            )
            block: Node = {"type": "codeBlock"}
            if fence[1]:
                block["attrs"] = {"language": fence[1]}
            if code := "\n".join(lines[i + 1 : end]):
                block["content"] = [{"type": "text", "text": code}]
            blocks.append(block)
            i = end + 1
        elif heading := _HEADING.match(stripped) or _WIKI_HEADING.match(stripped):
            level = heading[1]
            blocks.append(
                {
                    "type": "heading",
                    "attrs": {"level": int(level) if level.isdigit() else len(level)},
                    "content": _inline(heading[2]),
                }
            )
            i += 1
        elif _RULE.match(stripped):
            blocks.append({"type": "rule"})
            i += 1
        elif _LIST_ITEM.match(line):
            end = i
            while (
                end < len(lines)
                and (
                    _LIST_ITEM.match(lines[end])
                    # An indented line continues the item above it.
                    or (lines[end].strip() and lines[end][:1] in " \t" and end > i)
                )
            ):
                end += 1
            blocks += _lists(lines[i:end])
            i = end
        else:
            end = i + 1
            while end < len(lines) and not _is_block_start(lines[end]):
                end += 1
            blocks.append(_paragraph(" ".join(part.strip() for part in lines[i:end])))
            i = end
    return blocks


def _lists(lines: list[str]) -> list[Node]:
    """Lines of one list run -> list nodes, nested by indentation."""
    items: list[tuple[int, bool, str]] = []
    for line in lines:
        if match := _LIST_ITEM.match(line):
            indent = len(match[1].expandtabs(4))
            items.append((indent, match[2][0].isdigit(), match[3]))
        elif items:
            indent, ordered, text = items[-1]
            items[-1] = (indent, ordered, f"{text} {line.strip()}")
    nodes, _ = _build_lists(items, 0)
    return nodes


def _build_lists(
    items: list[tuple[int, bool, str]], start: int
) -> tuple[list[Node], int]:
    """Consume the items at the indent of items[start] and everything nested
    deeper; a switch between bullets and numbers starts a new list."""
    level = items[start][0]
    lists: list[Node] = []
    i = start
    while i < len(items) and items[i][0] >= level:
        indent, ordered, text = items[i]
        if indent > level and lists:
            nested, i = _build_lists(items, i)
            lists[-1]["content"][-1]["content"] += nested
            continue
        kind = "orderedList" if ordered else "bulletList"
        if not lists or lists[-1]["type"] != kind:
            lists.append({"type": kind, "content": []})
        lists[-1]["content"].append({"type": "listItem", "content": [_paragraph(text)]})
        i += 1
    return lists, i
