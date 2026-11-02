"""Tests for the ADF → Markdown converter."""

from typing import Any

from jira_mcp.adf import adf_to_markdown


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


def para(*inline: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(inline)}


def text(value: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def test_none_and_plain_string() -> None:
    assert adf_to_markdown(None) == ""
    assert adf_to_markdown("already text") == "already text"


def test_paragraphs_joined_by_blank_line() -> None:
    result = adf_to_markdown(doc(para(text("one")), para(text("two"))))
    assert result == "one\n\ntwo"


def test_heading_levels() -> None:
    result = adf_to_markdown(
        doc({"type": "heading", "attrs": {"level": 2}, "content": [text("Title")]})
    )
    assert result == "## Title"


def test_marks() -> None:
    result = adf_to_markdown(
        doc(
            para(
                text("bold", {"type": "strong"}),
                text(" "),
                text("italic", {"type": "em"}),
                text(" "),
                text("mono", {"type": "code"}),
                text(" "),
                text("gone", {"type": "strike"}),
            )
        )
    )
    assert result == "**bold** *italic* `mono` ~~gone~~"


def test_link_mark() -> None:
    result = adf_to_markdown(
        doc(para(text("docs", {"type": "link", "attrs": {"href": "https://x.io"}})))
    )
    assert result == "[docs](https://x.io)"


def test_bullet_list() -> None:
    result = adf_to_markdown(
        doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [para(text("a"))]},
                    {"type": "listItem", "content": [para(text("b"))]},
                ],
            }
        )
    )
    assert result == "- a\n- b"


def test_nested_ordered_list() -> None:
    inner = {
        "type": "orderedList",
        "content": [{"type": "listItem", "content": [para(text("inner"))]}],
    }
    outer = {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [para(text("outer")), inner]}],
    }
    result = adf_to_markdown(doc(outer))
    assert result == "- outer\n  1. inner"


def test_code_block_preserves_language() -> None:
    result = adf_to_markdown(
        doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [text("print('hi')")],
            }
        )
    )
    assert result == "```python\nprint('hi')\n```"


def test_blockquote() -> None:
    result = adf_to_markdown(
        doc({"type": "blockquote", "content": [para(text("quoted"))]})
    )
    assert result == "> quoted"


def test_hard_break_mention_emoji_rule() -> None:
    result = adf_to_markdown(
        doc(
            para(
                text("hi"),
                {"type": "hardBreak"},
                {"type": "mention", "attrs": {"text": "@Ada"}},
                text(" "),
                {"type": "emoji", "attrs": {"shortName": ":smile:", "text": "😄"}},
            ),
            {"type": "rule"},
        )
    )
    assert result == "hi\n@Ada 😄\n\n---"


def test_mention_without_at_prefix() -> None:
    result = adf_to_markdown(doc(para({"type": "mention", "attrs": {"text": "Ada"}})))
    assert result == "@Ada"


def test_table_with_header_row() -> None:
    def cell(kind: str, value: str) -> dict[str, Any]:
        return {"type": kind, "content": [para(text(value))]}

    table = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [cell("tableHeader", "K"), cell("tableHeader", "V")],
            },
            {
                "type": "tableRow",
                "content": [cell("tableCell", "a"), cell("tableCell", "1")],
            },
        ],
    }
    result = adf_to_markdown(doc(table))
    assert result == "| K | V |\n|---|---|\n| a | 1 |"


def test_inline_card_renders_url() -> None:
    result = adf_to_markdown(
        doc(
            para(
                {
                    "type": "inlineCard",
                    "attrs": {"url": "https://x.atlassian.net/browse/P-1"},
                }
            )
        )
    )
    assert result == "<https://x.atlassian.net/browse/P-1>"


def test_panel_becomes_labelled_blockquote() -> None:
    result = adf_to_markdown(
        doc(
            {
                "type": "panel",
                "attrs": {"panelType": "warning"},
                "content": [para(text("careful"))],
            }
        )
    )
    assert result == "> **Warning:**\n> careful"


def test_unknown_node_recurses_into_children() -> None:
    result = adf_to_markdown(
        doc({"type": "futureWidget", "content": [para(text("still visible"))]})
    )
    assert result == "still visible"


def test_unknown_leaf_node_is_dropped_silently() -> None:
    assert adf_to_markdown(doc({"type": "mysteryLeaf"})) == ""


def test_task_list_with_nested_tasks() -> None:
    task_list = {
        "type": "taskList",
        "content": [
            {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [text("ship")]},
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"state": "TODO"},
                        "content": [text("verify")],
                    }
                ],
            },
        ],
    }
    assert adf_to_markdown(doc(task_list)) == "- [x] ship\n  - [ ] verify"


def test_decision_list() -> None:
    decisions = {
        "type": "decisionList",
        "content": [{"type": "decisionItem", "content": [text("Use Postgres")]}],
    }
    assert adf_to_markdown(doc(decisions)) == "- **Decision:** Use Postgres"


def test_jira_macro_keeps_issue_key() -> None:
    macro = {
        "type": "extension",
        "attrs": {
            "extensionKey": "jira",
            "parameters": {"macroParams": {"key": {"value": "SHOP-42"}}},
        },
    }
    assert adf_to_markdown(doc(macro)) == "[Jira issue: SHOP-42]"


def test_jira_jql_macro_keeps_query() -> None:
    macro = {
        "type": "extension",
        "attrs": {
            "extensionKey": "jira",
            "parameters": {"macroParams": {"jqlQuery": {"value": "project = SHOP"}}},
        },
    }
    assert adf_to_markdown(doc(macro)) == "[Jira issues: project = SHOP]"


def test_other_macro_shows_its_title() -> None:
    macro = {
        "type": "extension",
        "attrs": {
            "extensionKey": "toc",
            "parameters": {"macroMetadata": {"title": "Table of Contents"}},
        },
    }
    assert adf_to_markdown(doc(macro)) == "[macro: Table of Contents]"


def test_bodied_macro_keeps_its_content() -> None:
    macro = {
        "type": "bodiedExtension",
        "attrs": {"extensionKey": "details"},
        "content": [para(text("inside"))],
    }
    assert adf_to_markdown(doc(macro)) == "inside"


def test_external_image_and_caption() -> None:
    image = {
        "type": "mediaSingle",
        "content": [
            {
                "type": "media",
                "attrs": {"type": "external", "url": "https://x.io/a.png"},
            },
            {"type": "caption", "content": [text("The "), text("flow")]},
        ],
    }
    assert adf_to_markdown(doc(image)) == "[image: https://x.io/a.png]\n\n*The flow*"


def test_inline_file_keeps_its_name() -> None:
    inline_file = {
        "type": "mediaInline",
        "attrs": {"id": "uuid-1", "type": "file", "alt": "notes.txt"},
    }
    assert adf_to_markdown(doc(para(text("see "), inline_file))) == (
        "see [attachment: notes.txt]"
    )
