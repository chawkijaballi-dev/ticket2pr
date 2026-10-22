from __future__ import annotations

from ticket2pr.trackers import adf


def test_to_text_handles_none_and_plain_strings() -> None:
    assert adf.to_text(None) == ""
    assert adf.to_text("already text") == "already text"


def test_to_text_renders_common_nodes() -> None:
    document = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Bug"}],
            },
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Call "},
                    {"type": "text", "text": "add()", "marks": [{"type": "code"}]},
                    {"type": "hardBreak"},
                    {"type": "mention", "attrs": {"text": "@ana"}},
                    {"type": "text", "text": " "},
                    {"type": "inlineCard", "attrs": {"url": "https://x.test"}},
                    {"type": "emoji", "attrs": {"text": ":)"}},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "one"}]}
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "two"}]}
                        ],
                    },
                ],
            },
            {
                "type": "orderedList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}
                        ],
                    }
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "py"},
                "content": [{"type": "text", "text": "x = 1"}],
            },
            {"type": "rule"},
            {"type": "unknownWrapper", "content": [{"type": "text", "text": "kept"}]},
        ],
    }
    text = adf.to_text(document)
    assert text.startswith("## Bug")
    assert "Call `add()`\n@ana https://x.test:)" in text
    assert "- one\n- two" in text
    assert "1. first" in text
    assert "```py\nx = 1\n```" in text
    assert "---" in text
    assert text.endswith("kept")


def test_from_markdown_builds_paragraphs_lists_links_and_code() -> None:
    doc = adf.from_markdown(
        "Opened [PR #3](https://github.com/a/b/pull/3)\nsecond line\n\n"
        "- Cost: `$1.20`\n- Turns: `4`"
    )
    assert doc["type"] == "doc"
    paragraph, bullets = doc["content"]
    assert paragraph["type"] == "paragraph"
    link = paragraph["content"][1]
    assert link == {
        "type": "text",
        "text": "PR #3",
        "marks": [{"type": "link", "attrs": {"href": "https://github.com/a/b/pull/3"}}],
    }
    assert {"type": "hardBreak"} in paragraph["content"]
    assert bullets["type"] == "bulletList"
    first_item = bullets["content"][0]["content"][0]["content"]
    assert first_item[0] == {"type": "text", "text": "Cost: "}
    assert first_item[1] == {"type": "text", "text": "$1.20", "marks": [{"type": "code"}]}


def test_round_trip_keeps_the_text() -> None:
    markdown = "Hello `world`\n\n- a\n- b"
    assert adf.to_text(adf.from_markdown(markdown)) == markdown
