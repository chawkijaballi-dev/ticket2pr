"""Minimal Atlassian Document Format (ADF) conversion.

Jira Cloud's REST API v3 returns and expects rich text as ADF JSON. The agent only needs
readable text, and our comments only use paragraphs, bullet lists, links and inline code,
so a full converter is unnecessary.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_BLOCK_NODES = {"paragraph", "blockquote", "panel"}
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_CODE_RE = re.compile(r"`([^`]+)`")


def to_text(node: Any) -> str:
    """Flatten an ADF document (or plain string, or ``None``) into Markdown-ish text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return _render(node).strip()


def _render(node: dict[str, Any], depth: int = 0) -> str:
    kind = node.get("type")
    attrs: dict[str, Any] = node.get("attrs") or {}
    children: list[dict[str, Any]] = node.get("content") or []
    inner = "".join(_render(child, depth) for child in children)

    if kind == "text":
        text: str = node.get("text", "")
        marks = {mark.get("type") for mark in node.get("marks") or []}
        return f"`{text}`" if "code" in marks else text
    if kind in _ATOMS:
        return _ATOMS[kind](attrs)
    if kind in {"bulletList", "orderedList"}:
        return _render_list(children, ordered=kind == "orderedList", depth=depth)
    if kind == "codeBlock":
        return f"```{attrs.get('language') or ''}\n{inner}\n```\n\n"
    if kind == "heading":
        return "#" * int(attrs.get("level", 1)) + f" {inner}\n\n"
    return inner + "\n\n" if kind in _BLOCK_NODES else inner


def _render_list(items: list[dict[str, Any]], *, ordered: bool, depth: int) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        bullet = f"{index}." if ordered else "-"
        body = "".join(_render(child, depth + 1) for child in item.get("content") or [])
        lines.append(f"{'  ' * depth}{bullet} {body.strip()}")
    return "\n".join(lines) + "\n\n"


_ATOMS: dict[str, Callable[[dict[str, Any]], str]] = {
    "hardBreak": lambda _: "\n",
    "rule": lambda _: "---\n\n",
    "mention": lambda attrs: str(attrs.get("text", "")),
    "emoji": lambda attrs: str(attrs.get("text", "")),
    "inlineCard": lambda attrs: str(attrs.get("url", "")),
}


def from_markdown(markdown: str) -> dict[str, Any]:
    """Build an ADF document from the small Markdown subset ticket2pr writes.

    Supported: paragraphs (blank-line separated), ``- `` bullet lists, ``[text](url)`` links
    and inline code. Anything else is kept as literal text.
    """
    blocks: list[dict[str, Any]] = []
    for chunk in re.split(r"\n\s*\n", markdown.strip()):
        lines = chunk.splitlines()
        if lines and all(line.lstrip().startswith("- ") for line in lines):
            items = [
                {"type": "listItem", "content": [_paragraph(line.lstrip()[2:])]} for line in lines
            ]
            blocks.append({"type": "bulletList", "content": items})
        elif chunk:
            blocks.append(_paragraph(chunk))
    return {"type": "doc", "version": 1, "content": blocks}


def _paragraph(text: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines()):
        if index:
            content.append({"type": "hardBreak"})
        content.extend(_inline(line))
    return {"type": "paragraph", "content": content}


def _inline(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    pattern = re.compile(f"{_LINK_RE.pattern}|{_CODE_RE.pattern}")
    for match in pattern.finditer(text):
        if match.start() > position:
            nodes.append({"type": "text", "text": text[position : match.start()]})
        label, url, code = match.groups()
        if code is not None:
            nodes.append({"type": "text", "text": code, "marks": [{"type": "code"}]})
        else:
            mark = {"type": "link", "attrs": {"href": url}}
            nodes.append({"type": "text", "text": label, "marks": [mark]})
        position = match.end()
    if position < len(text):
        nodes.append({"type": "text", "text": text[position:]})
    return nodes
