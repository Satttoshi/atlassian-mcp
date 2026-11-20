"""Tests for the Markdown to ADF converter."""

from typing import Any

from jira_mcp.adf import adf_to_markdown
from jira_mcp.markdown_adf import markdown_to_adf


def content(markdown: str) -> list[dict[str, Any]]:
    doc = markdown_to_adf(markdown)
    assert doc["type"] == "doc" and doc["version"] == 1
    return list(doc["content"])


def test_headings_markdown_and_wiki() -> None:
    blocks = content("## Features\nh3. Fixes\n# Top #")
    assert [(b["type"], b["attrs"]["level"]) for b in blocks] == [
        ("heading", 2),
        ("heading", 3),
        ("heading", 1),
    ]
    assert blocks[2]["content"] == [{"type": "text", "text": "Top"}]


def test_inline_marks_and_links() -> None:
    (paragraph,) = content(
        "A **bold [PR](https://x.io/1)**, *it*, _em_, `co_de`, snake_case_name, "
        "[OM-1|https://j.io/OM-1] and https://x.io/2."
    )
    nodes = paragraph["content"]
    marks = {n["text"]: [m["type"] for m in n.get("marks", [])] for n in nodes}
    assert marks["bold "] == ["strong"]
    assert marks["PR"] == ["strong", "link"]
    assert marks["it"] == ["em"] and marks["em"] == ["em"]
    assert marks["co_de"] == ["code"]
    assert marks[", snake_case_name, "] == []
    links = {
        n["text"]: m["attrs"]["href"]
        for n in nodes
        for m in n.get("marks", [])
        if m["type"] == "link"
    }
    assert links == {
        "PR": "https://x.io/1",
        "OM-1": "https://j.io/OM-1",
        "https://x.io/2": "https://x.io/2",
    }
    assert nodes[-1] == {"type": "text", "text": "."}


def test_nested_and_mixed_lists() -> None:
    blocks = content(
        "- one\n  - nested\n    continued\n  - nested 2\n- two\n1. first\n2. second"
    )
    assert [b["type"] for b in blocks] == ["bulletList", "orderedList"]
    one, two = blocks[0]["content"]
    assert [c["type"] for c in one["content"]] == ["paragraph", "bulletList"]
    nested = one["content"][1]["content"]
    assert nested[0]["content"][0]["content"][0]["text"] == "nested continued"
    assert len(nested) == 2 and len(two["content"]) == 1
    assert len(blocks[1]["content"]) == 2


def test_code_block_rule_and_paragraphs() -> None:
    blocks = content("line one\nline two\n\n---\n```ts\nconst a = *1*;\n```\nafter")
    assert [b["type"] for b in blocks] == [
        "paragraph",
        "rule",
        "codeBlock",
        "paragraph",
    ]
    assert blocks[0]["content"][0]["text"] == "line one line two"
    assert blocks[2]["attrs"] == {"language": "ts"}
    assert blocks[2]["content"] == [{"type": "text", "text": "const a = *1*;"}]


def test_round_trip_through_the_renderer() -> None:
    markdown = (
        "## Features\n\n- one ([OM-1](https://x.io/OM-1))\n  - nested\n- two\n\n"
        "1. first\n2. second\n\nDone with **bold**."
    )
    assert adf_to_markdown(markdown_to_adf(markdown)).strip() == markdown
    assert content("") == [] and content("  \n\n") == []
