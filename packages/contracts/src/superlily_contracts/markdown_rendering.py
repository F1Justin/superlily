"""Bounded Markdown source conversion for the Phase 4 renderer.

The model-facing format is ordinary Markdown. This module deterministically
lowers a deliberately small, inert subset into the reviewed RenderDocument AST;
it never emits HTML, follows links, or resolves image and file references.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .rendering import RenderDocument


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$")
_UNORDERED_RE = re.compile(r"^\s{0,3}[-+*][ \t]+(.+)$")
_ORDERED_RE = re.compile(r"^\s{0,3}\d{1,9}[.)][ \t]+(.+)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>[ \t]?(.*)$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})([^`]*)$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z0-9_+.-]{1,32}$")
_TABLE_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MarkdownDocumentIn(_StrictModel):
    """An inert Markdown document submitted by a trusted bridge identity."""

    schema_version: Literal["1.0"] = "1.0"
    instance_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    conversation_key: str = Field(min_length=1, max_length=320)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    markdown: str = Field(min_length=1, max_length=24_000)


class MarkdownRenderingError(ValueError):
    """A stable, non-sensitive Markdown conversion failure."""

    def __init__(self, code: str, safe_detail: str):
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


def _table_cells(line: str) -> list[str]:
    """Split a simple pipe table row without interpreting inline markup."""

    value = line.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith(r"\|"):
        value = value[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in value:
        if escaped:
            if character in {"|", "\\"}:
                current.append(character)
            else:
                current.extend(("\\", character))
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(line: str, width: int) -> bool:
    cells = _table_cells(line)
    return (
        len(cells) == width
        and 1 <= width <= 8
        and all(_TABLE_SEPARATOR_CELL_RE.fullmatch(cell) for cell in cells)
    )


def _split_bounded_paragraph(value: str) -> list[str]:
    """Split only when needed to satisfy the RenderDocument text bound."""

    remaining = value.strip()
    parts: list[str] = []
    while len(remaining) > 4_000:
        split_at = max(
            remaining.rfind("\n", 0, 4_001),
            remaining.rfind(" ", 0, 4_001),
        )
        if split_at < 1:
            split_at = 4_000
        parts.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def markdown_to_render_document(payload: MarkdownDocumentIn) -> RenderDocument:
    """Lower safe block Markdown to a versioned RenderDocument.

    Supported block syntax is headings, paragraphs, ordered/unordered lists,
    block quotes, fenced code, display math delimited by standalone ``$$``,
    and simple pipe tables. Paired ``**strong**`` and single-dollar inline math
    remain in text fields for the reviewed inline renderer. All other Markdown,
    including raw HTML, links, and image syntax, remains escaped literal text.
    """

    if "\x00" in payload.markdown:
        raise MarkdownRenderingError(
            "markdown_contains_nul",
            "Markdown source contains a forbidden NUL byte",
        )
    lines = payload.markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[dict] = []
    counter = 0

    def append(kind: str, **fields) -> None:
        nonlocal counter
        counter += 1
        if counter > 64:
            raise MarkdownRenderingError(
                "markdown_block_limit",
                "Markdown source exceeds the block limit",
            )
        blocks.append({"kind": kind, "node_id": f"md{counter:03d}", **fields})

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group(1)
            language_hint = (
                fence.group(2).strip().split(maxsplit=1)[0]
                if fence.group(2).strip()
                else ""
            )
            language = (
                language_hint if _LANGUAGE_RE.fullmatch(language_hint) else None
            )
            index += 1
            code_lines: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if re.match(
                    rf"^\s{{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$",
                    candidate,
                ):
                    index += 1
                    break
                code_lines.append(candidate)
                index += 1
            code = "\n".join(code_lines) or " "
            if len(code) > 8_000:
                raise MarkdownRenderingError(
                    "markdown_code_limit",
                    "Markdown code fence exceeds the code limit",
                )
            append("code", code=code, language=language, wrap=True)
            continue

        stripped = line.strip()
        inline_display = (
            stripped.startswith("$$")
            and stripped.endswith("$$")
            and len(stripped) > 4
        )
        if stripped == "$$" or inline_display:
            if stripped == "$$":
                index += 1
                math_lines: list[str] = []
                while index < len(lines) and lines[index].strip() != "$$":
                    math_lines.append(lines[index])
                    index += 1
                if index >= len(lines):
                    raise MarkdownRenderingError(
                        "markdown_math_unclosed",
                        "Markdown display math delimiter is not closed",
                    )
                index += 1
                latex = "\n".join(math_lines).strip()
            else:
                latex = stripped[2:-2].strip()
                index += 1
            if not latex:
                raise MarkdownRenderingError(
                    "markdown_math_empty",
                    "Markdown display math cannot be empty",
                )
            append("math", latex=latex, display=True)
            continue

        heading = _HEADING_RE.match(line)
        if heading is not None:
            append(
                "heading",
                text=heading.group(2),
                level=1 if len(heading.group(1)) == 1 else 2,
            )
            index += 1
            continue

        unordered = _UNORDERED_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if unordered is not None or ordered is not None:
            is_ordered = ordered is not None
            matcher = _ORDERED_RE if is_ordered else _UNORDERED_RE
            items: list[str] = []
            while index < len(lines):
                item = matcher.match(lines[index])
                if item is None:
                    break
                text = item.group(1).strip()
                if not text or len(text) > 2_000:
                    raise MarkdownRenderingError(
                        "markdown_list_item_limit",
                        "Markdown list item is empty or too long",
                    )
                items.append(text)
                index += 1
                if len(items) > 32:
                    raise MarkdownRenderingError(
                        "markdown_list_limit",
                        "Markdown list exceeds the item limit",
                    )
            append("list", ordered=is_ordered, items=items)
            continue

        quote = _QUOTE_RE.match(line)
        if quote is not None:
            quote_lines: list[str] = []
            while index < len(lines):
                item = _QUOTE_RE.match(lines[index])
                if item is None:
                    break
                quote_lines.append(item.group(1))
                index += 1
            for part in _split_bounded_paragraph("\n".join(quote_lines)):
                append("quote", text=part)
            continue

        header_cells = _table_cells(line) if "|" in line else []
        if (
            header_cells
            and index + 1 < len(lines)
            and _is_table_separator(lines[index + 1], len(header_cells))
            and all(header_cells)
        ):
            if any(len(cell) > 512 for cell in header_cells):
                raise MarkdownRenderingError(
                    "markdown_table_cell_limit",
                    "Markdown table cell exceeds the size limit",
                )
            rows: list[list[str]] = []
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                row = _table_cells(lines[index])
                if len(row) != len(header_cells):
                    break
                if any(not cell or len(cell) > 512 for cell in row):
                    raise MarkdownRenderingError(
                        "markdown_table_cell_limit",
                        "Markdown table cell is empty or too long",
                    )
                rows.append(row)
                index += 1
                if len(rows) > 32:
                    raise MarkdownRenderingError(
                        "markdown_table_row_limit",
                        "Markdown table exceeds the row limit",
                    )
            if not rows:
                for part in _split_bounded_paragraph(line):
                    append("paragraph", text=part)
            else:
                append("table", columns=header_cells, rows=rows)
            continue

        paragraph_lines = [line.strip()]
        index += 1
        while index < len(lines) and lines[index].strip():
            candidate = lines[index]
            candidate_stripped = candidate.strip()
            candidate_display = (
                candidate_stripped.startswith("$$")
                and candidate_stripped.endswith("$$")
                and len(candidate_stripped) > 4
            )
            if (
                _FENCE_RE.match(candidate)
                or _HEADING_RE.match(candidate)
                or _UNORDERED_RE.match(candidate)
                or _ORDERED_RE.match(candidate)
                or _QUOTE_RE.match(candidate)
                or candidate_stripped == "$$"
                or candidate_display
            ):
                break
            paragraph_lines.append(candidate.strip())
            index += 1
        for part in _split_bounded_paragraph("\n".join(paragraph_lines)):
            append("paragraph", text=part)

    if not blocks:
        raise MarkdownRenderingError(
            "markdown_empty",
            "Markdown source does not contain renderable content",
        )
    try:
        return RenderDocument(
            schema_version="1.3",
            instance_id=payload.instance_id,
            conversation_key=payload.conversation_key,
            source_event_id=payload.source_event_id,
            title=payload.title,
            blocks=blocks,
        )
    except ValueError as exc:
        raise MarkdownRenderingError(
            "markdown_document_invalid",
            "Markdown source exceeds a renderer safety boundary",
        ) from exc
