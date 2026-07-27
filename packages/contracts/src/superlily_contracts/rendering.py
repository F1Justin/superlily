"""Versioned contracts for deterministic chat-document rendering and delivery."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonicalize_json_value


RENDER_DOCUMENT_SCHEMA_VERSION = "1.3"
_CONVERSATION_KEY_RE = re.compile(r"^[a-z0-9_]+-(?:group|private)_[A-Za-z0-9.-]{1,256}$")
_FORBIDDEN_LATEX_RE = re.compile(
    r"\\(?:input|include|openin|openout|read|write|usepackage|documentclass|"
    r"newcommand|renewcommand|def|catcode|csname|endcsname|special|immediate|"
    r"write18|includegraphics|href|url|font|usefont|fontspec|setmainfont|"
    r"setsansfont|setmonofont|setmathfont|setcjkmainfont|setcjksansfont|"
    r"setcjkmonofont|setcjkmathfont|pdfmapfile|pdffontattr|directlua|"
    r"everyjob|loop|repeat|futurelet)\b",
    re.IGNORECASE,
)


def split_inline_math(value: str) -> tuple[tuple[Literal["text", "math"], str], ...]:
    r"""Split prose containing safe single-dollar inline math.

    Escaped ``\$`` is treated as a literal dollar, while an unmatched dollar is
    ordinary prose. Display math remains an explicit ``MathBlock``.
    """

    segments: list[tuple[Literal["text", "math"], str]] = []
    text: list[str] = []
    index = 0
    while index < len(value):
        if value[index : index + 2] == r"\$":
            text.append("$")
            index += 2
            continue
        if value[index : index + 2] == "$$":
            text.append("$$")
            index += 2
            continue
        if value[index] != "$":
            text.append(value[index])
            index += 1
            continue

        closing = index + 1
        while closing < len(value):
            if value[closing : closing + 2] == r"\$":
                closing += 2
                continue
            if value[closing] == "$":
                break
            closing += 1
        latex = value[index + 1 : closing] if closing < len(value) else ""
        if closing >= len(value) or not latex.strip():
            text.append("$")
            index += 1
            continue
        if text:
            segments.append(("text", "".join(text)))
            text = []
        segments.append(("math", latex))
        index = closing + 1
    if text:
        segments.append(("text", "".join(text)))
    return tuple(segments)


def split_inline_content(
    value: str,
    *,
    markdown_lite: bool = False,
) -> tuple[tuple[Literal["text", "math", "strong"], str], ...]:
    r"""Split reviewed inline content into text, math, and optional strong runs.

    RenderDocument 1.2 deliberately recognizes only paired ``**strong**``
    markers. Block Markdown, raw HTML, links, images, and nested emphasis remain
    ordinary escaped text. Unmatched or empty markers are also literal text so
    a cosmetic model mistake cannot fail the whole render request.
    """

    if not markdown_lite:
        return split_inline_math(value)

    def math_closing(start: int) -> int | None:
        closing = start + 1
        while closing < len(value):
            if value[closing : closing + 2] == r"\$":
                closing += 2
                continue
            if value[closing] == "$":
                return closing
            closing += 1
        return None

    def strong_closing(start: int) -> int | None:
        closing = start
        while closing < len(value):
            if value[closing : closing + 2] == r"\$":
                closing += 2
                continue
            if value[closing : closing + 2] == "$$":
                closing += 2
                continue
            if value[closing] == "$":
                math_end = math_closing(closing)
                if math_end is not None and value[closing + 1 : math_end].strip():
                    closing = math_end + 1
                    continue
            if value[closing : closing + 2] == "**":
                return closing
            closing += 1
        return None

    segments: list[tuple[Literal["text", "math", "strong"], str]] = []
    text: list[str] = []
    index = 0
    while index < len(value):
        if value[index : index + 2] == r"\$":
            text.append("$")
            index += 2
            continue
        if value[index : index + 2] == "$$":
            text.append("$$")
            index += 2
            continue
        if value[index] == "$":
            closing = math_closing(index)
            latex = value[index + 1 : closing] if closing is not None else ""
            if closing is not None and latex.strip():
                if text:
                    segments.append(("text", "".join(text)))
                    text = []
                segments.append(("math", latex))
                index = closing + 1
                continue
        if value[index : index + 2] == "**":
            closing = strong_closing(index + 2)
            strong = value[index + 2 : closing] if closing is not None else ""
            if closing is not None and strong.strip():
                if text:
                    segments.append(("text", "".join(text)))
                    text = []
                segments.append(("strong", strong))
                index = closing + 2
                continue
            text.append("**")
            index += 2
            continue
        text.append(value[index])
        index += 1
    if text:
        segments.append(("text", "".join(text)))
    return tuple(segments)


def inline_content_plain_text(value: str, *, markdown_lite: bool = False) -> str:
    """Return semantic plain text without reviewed presentation markers."""

    parts: list[str] = []
    for kind, content in split_inline_content(value, markdown_lite=markdown_lite):
        if kind == "math":
            parts.append(f"${content}$")
        elif kind == "strong":
            parts.append(inline_content_plain_text(content, markdown_lite=False))
        else:
            parts.append(content)
    return "".join(parts)


def _validate_latex_syntax(value: str) -> None:
    if any(
        (ord(character) < 32 and character != "\n") or ord(character) == 127
        for character in value
    ):
        raise ValueError("LaTeX contains a forbidden control character")
    brace_depth = 0
    for index, character in enumerate(value):
        if character not in {"{", "}"}:
            continue
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and value[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2:
            continue
        if character == "{":
            brace_depth += 1
        else:
            brace_depth -= 1
            if brace_depth < 0:
                raise ValueError("LaTeX contains an unmatched closing brace")
    if brace_depth:
        raise ValueError("LaTeX contains an unmatched opening brace")


def _validate_mixed_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text contains a NUL byte")
    for kind, content in split_inline_math(value):
        if kind == "math":
            _validate_latex_syntax(content)
            if len(content) > 2_000 or _FORBIDDEN_LATEX_RE.search(content):
                raise ValueError("inline math contains a forbidden or oversized LaTeX expression")
    return value


def _validate_optional_mixed_text(value: str | None) -> str | None:
    return _validate_mixed_text(value) if value is not None else None


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _RenderNode(_StrictModel):
    node_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    accessibility_text: str | None = Field(default=None, min_length=1, max_length=2_000)

    @field_validator("accessibility_text")
    @classmethod
    def validate_accessibility_inline_math(cls, value: str | None) -> str | None:
        return _validate_mixed_text(value) if value is not None else None


class TextBlock(_RenderNode):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4_000)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)


class ParagraphBlock(_RenderNode):
    kind: Literal["paragraph"] = "paragraph"
    text: str = Field(min_length=1, max_length=4_000)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)


class HeadingBlock(_RenderNode):
    kind: Literal["heading"] = "heading"
    text: str = Field(min_length=1, max_length=240)
    level: Literal[1, 2] = 1

    _validate_inline_math = field_validator("text")(_validate_mixed_text)


class MathBlock(_RenderNode):
    kind: Literal["math"] = "math"
    latex: str = Field(min_length=1, max_length=2_000)
    display: bool = True

    @model_validator(mode="after")
    def reject_unsafe_commands(self) -> "MathBlock":
        _validate_latex_syntax(self.latex)
        if "\x00" in self.latex or _FORBIDDEN_LATEX_RE.search(self.latex):
            raise ValueError("math block contains a forbidden LaTeX command")
        return self


class ListBlock(_RenderNode):
    kind: Literal["list"] = "list"
    ordered: bool = False
    items: list[str] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_items(self) -> "ListBlock":
        if any(not item.strip() or len(item) > 2_000 or "\x00" in item for item in self.items):
            raise ValueError("list items must be non-empty bounded text")
        for item in self.items:
            _validate_mixed_text(item)
        return self


class QuoteBlock(_RenderNode):
    kind: Literal["quote"] = "quote"
    text: str = Field(min_length=1, max_length=4_000)
    attribution: str | None = Field(default=None, min_length=1, max_length=240)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)
    _validate_optional_inline_math = field_validator("attribution")(
        _validate_optional_mixed_text
    )


class CodeBlock(_RenderNode):
    kind: Literal["code"] = "code"
    code: str = Field(min_length=1, max_length=8_000)
    language: str | None = Field(default=None, min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_+.-]+$")
    wrap: bool = True

    @field_validator("code")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("code contains a NUL byte")
        return value


class TableBlock(_RenderNode):
    kind: Literal["table"] = "table"
    columns: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_cells(self) -> "TableBlock":
        width = len(self.columns)
        cells = [*self.columns, *(cell for row in self.rows for cell in row)]
        if any(len(row) != width for row in self.rows):
            raise ValueError("table rows must match the column count")
        if any(not cell.strip() or len(cell) > 512 for cell in cells):
            raise ValueError("table cells must be non-empty bounded text")
        for cell in cells:
            _validate_mixed_text(cell)
        return self


class NoticeBlock(_RenderNode):
    kind: Literal["notice"] = "notice"
    severity: Literal["info", "warning", "error"] = "info"
    title: str | None = Field(default=None, min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2_000)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)
    _validate_optional_inline_math = field_validator("title")(_validate_optional_mixed_text)


class WarningBlock(_RenderNode):
    kind: Literal["warning"] = "warning"
    title: str | None = Field(default=None, min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=2_000)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)
    _validate_optional_inline_math = field_validator("title")(
        _validate_optional_mixed_text
    )


class ErrorSummaryBlock(_RenderNode):
    kind: Literal["error_summary"] = "error_summary"
    title: str = Field(default="执行失败", min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1_000)
    items: list[str] = Field(default_factory=list, max_length=16)

    _validate_title_inline_math = field_validator("title")(_validate_mixed_text)
    _validate_summary_inline_math = field_validator("summary")(_validate_mixed_text)

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 500 for item in value):
            raise ValueError("error summary items must be non-empty bounded text")
        for item in value:
            _validate_mixed_text(item)
        return value


class CardField(_StrictModel):
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=500)

    _validate_label_inline_math = field_validator("label")(_validate_mixed_text)
    _validate_value_inline_math = field_validator("value")(_validate_mixed_text)


class CardAction(_StrictModel):
    action_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    label: str = Field(min_length=1, max_length=120)

    _validate_label_inline_math = field_validator("label")(_validate_mixed_text)


class CardBlock(_RenderNode):
    kind: Literal["card"] = "card"
    title: str = Field(min_length=1, max_length=160)
    status: Literal["neutral", "info", "success", "warning", "error"] = "neutral"
    body: str | None = Field(default=None, min_length=1, max_length=2_000)
    fields: list[CardField] = Field(default_factory=list, max_length=16)
    actions: list[CardAction] = Field(default_factory=list, max_length=8)

    _validate_title_inline_math = field_validator("title")(_validate_mixed_text)
    _validate_body_inline_math = field_validator("body")(_validate_optional_mixed_text)

    @model_validator(mode="after")
    def validate_card(self) -> "CardBlock":
        if not self.body and not self.fields:
            raise ValueError("card requires body or fields")
        action_ids = [item.action_id for item in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("card action identifiers must be unique")
        return self


class ProgressBlock(_RenderNode):
    kind: Literal["progress"] = "progress"
    label: str = Field(min_length=1, max_length=120)
    value: int = Field(ge=0, le=100)
    detail: str | None = Field(default=None, min_length=1, max_length=500)

    _validate_inline_math = field_validator("label")(_validate_mixed_text)
    _validate_optional_inline_math = field_validator("detail")(_validate_optional_mixed_text)


class ImageBlock(_RenderNode):
    kind: Literal["image"] = "image"
    artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    caption: str | None = Field(default=None, min_length=1, max_length=500)

    _validate_inline_math = field_validator("caption")(_validate_optional_mixed_text)


class ArtifactRefBlock(_RenderNode):
    kind: Literal["artifact_ref"] = "artifact_ref"
    artifact_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    mime_type: str = Field(min_length=3, max_length=128, pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
    label: str = Field(min_length=1, max_length=500)

    _validate_inline_math = field_validator("label")(_validate_optional_mixed_text)


LeafRenderBlock = Annotated[
    TextBlock
    | ParagraphBlock
    | HeadingBlock
    | MathBlock
    | ListBlock
    | QuoteBlock
    | CodeBlock
    | TableBlock
    | NoticeBlock
    | WarningBlock
    | ErrorSummaryBlock
    | CardBlock
    | ProgressBlock
    | ImageBlock
    | ArtifactRefBlock,
    Field(discriminator="kind"),
]


class GroupBlock(_RenderNode):
    kind: Literal["group"] = "group"
    label: str | None = Field(default=None, min_length=1, max_length=120)
    blocks: list[LeafRenderBlock] = Field(min_length=1, max_length=32)

    _validate_inline_math = field_validator("label")(_validate_optional_mixed_text)


class AlternativeOption(_StrictModel):
    option_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=120)
    requires: list[str] = Field(default_factory=list, max_length=16)
    blocks: list[LeafRenderBlock] = Field(min_length=1, max_length=32)

    _validate_inline_math = field_validator("label")(_validate_mixed_text)

    @field_validator("requires")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"[a-z0-9_.-]{1,64}", item) for item in value
        ):
            raise ValueError("alternative capabilities must be unique bounded identifiers")
        return value


class AlternativeBlock(_RenderNode):
    kind: Literal["alternative"] = "alternative"
    options: list[AlternativeOption] = Field(min_length=1, max_length=8)
    preferred_option_id: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_preferred_option(self) -> "AlternativeBlock":
        ids = [option.option_id for option in self.options]
        if len(ids) != len(set(ids)) or self.preferred_option_id not in ids:
            raise ValueError("alternative options must be unique and include the preferred option")
        return self


RenderBlock = Annotated[
    TextBlock
    | ParagraphBlock
    | HeadingBlock
    | MathBlock
    | ListBlock
    | QuoteBlock
    | CodeBlock
    | TableBlock
    | NoticeBlock
    | WarningBlock
    | ErrorSummaryBlock
    | CardBlock
    | ProgressBlock
    | ImageBlock
    | ArtifactRefBlock
    | GroupBlock
    | AlternativeBlock,
    Field(discriminator="kind"),
]


class RenderDocument(_StrictModel):
    schema_version: Literal["1.0", "1.1", "1.2", "1.3"] = RENDER_DOCUMENT_SCHEMA_VERSION
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    conversation_key: str = Field(min_length=1, max_length=320)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    blocks: list[RenderBlock] = Field(min_length=1, max_length=64)

    @field_validator("title")
    @classmethod
    def validate_title_inline_math(cls, value: str | None) -> str | None:
        return _validate_mixed_text(value) if value is not None else None

    @model_validator(mode="after")
    def validate_document(self) -> "RenderDocument":
        if not _CONVERSATION_KEY_RE.fullmatch(self.conversation_key):
            raise ValueError("conversation_key must use adapter-group_id or adapter-private_id")
        nodes: list[_RenderNode] = []
        for block in self.blocks:
            nodes.append(block)
            if isinstance(block, GroupBlock):
                nodes.extend(block.blocks)
            elif isinstance(block, AlternativeBlock):
                nodes.extend(child for option in block.options for child in option.blocks)
        if len(nodes) > 128:
            raise ValueError("render document exceeds the node limit")
        if self.schema_version != "1.0":
            node_ids = [node.node_id for node in nodes]
            if any(node_id is None for node_id in node_ids) or len(node_ids) != len(set(node_ids)):
                raise ValueError("schema 1.1 and later require unique node_id values")
        artifact_nodes = [node for node in nodes if isinstance(node, (ImageBlock, ArtifactRefBlock))]
        if len(artifact_nodes) > 8:
            raise ValueError("render document exceeds the artifact reference limit")
        if any(not node.accessibility_text for node in artifact_nodes):
            raise ValueError("artifact nodes require accessibility_text")
        encoded = canonicalize_json_value(self.model_dump(mode="json")).canonical_bytes
        if len(encoded) > 32_768:
            raise ValueError("render document exceeds the canonical byte limit")
        return self


class RenderDocumentReceipt(_StrictModel):
    render_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    attempt_id: str | None = Field(default=None, min_length=36, max_length=36)
    delivery_plan_id: str | None = Field(default=None, min_length=36, max_length=36)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png"] = "image/png"
    byte_size: int = Field(ge=1, le=8_388_608)
    width_pixels: int = Field(ge=1, le=4_096)
    height_pixels: int = Field(ge=1, le=4_096)
    render_duration_ms: int = Field(ge=0, le=120_000)
    content_path: str
    duplicate: bool = False


class RenderArtifactDeletionIn(_StrictModel):
    instance_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    reason: Literal[
        "user_request",
        "retention_elapsed",
        "security_response",
        "test_cleanup",
    ]


class RenderArtifactDeletionReceipt(_StrictModel):
    artifact_id: str = Field(min_length=36, max_length=36)
    content_deleted: bool
    physical_object_removed: bool
    duplicate: bool = False


class DeliveryAttemptIn(_StrictModel):
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    outcome: Literal["succeeded", "failed", "ambiguous"]
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    safe_error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    delivery_plan_id: str | None = Field(default=None, min_length=36, max_length=36)
    delivery_intent_id: str | None = Field(default=None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> "DeliveryAttemptIn":
        if self.outcome == "succeeded" and (
            not self.platform_message_id or self.safe_error_code is not None
        ):
            raise ValueError("successful delivery requires only platform_message_id")
        if self.outcome != "succeeded" and not self.safe_error_code:
            raise ValueError("non-successful delivery requires safe_error_code")
        return self


class DeliveryIntentIn(_StrictModel):
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    delivery_plan_id: str = Field(min_length=36, max_length=36)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reply_to_platform_message_id: str | None = Field(
        default=None, min_length=1, max_length=512
    )
    mention_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("mention_ids")
    @classmethod
    def validate_mentions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not item
            or len(item) > 256
            or item != item.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
            for item in value
        ):
            raise ValueError("mention identifiers must be unique bounded visible strings")
        return value


class DeliveryIntentReceipt(_StrictModel):
    intent_id: str = Field(min_length=36, max_length=36)
    should_send: bool
    status: Literal["pending", "succeeded", "failed", "ambiguous"]
    duplicate: bool = False


class DeliveryCompletionIn(_StrictModel):
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    outcome: Literal["succeeded", "failed", "ambiguous"]
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    safe_error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def validate_completion(self) -> "DeliveryCompletionIn":
        DeliveryAttemptIn(
            instance_id=self.instance_id,
            outcome=self.outcome,
            platform_message_id=self.platform_message_id,
            safe_error_code=self.safe_error_code,
        )
        return self


class DeliveryPlanReceipt(_StrictModel):
    delivery_plan_id: str = Field(min_length=36, max_length=36)
    capability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_family: Literal["image", "text"]
    fallback_text: str | None = Field(default=None, max_length=8_000)
    degradation_reasons: list[str] = Field(default_factory=list, max_length=16)
    decision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    resolved_document_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selected_alternatives: list[dict] = Field(default_factory=list, max_length=64)
    rejected_alternatives: list[dict] = Field(default_factory=list, max_length=64)
    ordered_payloads: list[dict] = Field(default_factory=list, max_length=16)


def _leaf_plain_text(block: LeafRenderBlock, *, markdown_lite: bool) -> str:
    def plain(value: str) -> str:
        return inline_content_plain_text(value, markdown_lite=markdown_lite)

    if block.accessibility_text:
        return plain(block.accessibility_text)
    if isinstance(block, (TextBlock, ParagraphBlock, HeadingBlock, QuoteBlock)):
        return plain(block.text)
    if isinstance(block, MathBlock):
        return block.latex
    if isinstance(block, ListBlock):
        return "\n".join(f"- {plain(item)}" for item in block.items)
    if isinstance(block, CodeBlock):
        return block.code
    if isinstance(block, TableBlock):
        return "\n".join(
            " | ".join(plain(cell) for cell in row)
            for row in [block.columns, *block.rows]
        )
    if isinstance(block, NoticeBlock):
        return "：".join(plain(item) for item in (block.title, block.text) if item)
    if isinstance(block, WarningBlock):
        return "：".join(plain(item) for item in (block.title, block.text) if item)
    if isinstance(block, ErrorSummaryBlock):
        return "\n".join(
            [
                plain(block.title),
                plain(block.summary),
                *(plain(item) for item in block.items),
            ]
        )
    if isinstance(block, CardBlock):
        parts = [plain(block.title)]
        if block.body:
            parts.append(plain(block.body))
        parts.extend(
            f"{plain(field.label)}：{plain(field.value)}"
            for field in block.fields
        )
        parts.extend(f"[{plain(action.label)}]" for action in block.actions)
        return "\n".join(parts)
    if isinstance(block, ProgressBlock):
        return f"{plain(block.label)}：{block.value}%" + (
            f"（{plain(block.detail)}）" if block.detail else ""
        )
    if isinstance(block, ImageBlock):
        return plain(block.caption) if block.caption else "图片"
    if isinstance(block, ArtifactRefBlock):
        return plain(block.label)
    raise TypeError("unsupported render block")


def render_document_plain_text(document: RenderDocument) -> str:
    markdown_lite = document.schema_version in {"1.2", "1.3"}

    def plain(value: str) -> str:
        return inline_content_plain_text(value, markdown_lite=markdown_lite)

    parts = [plain(document.title)] if document.title else []
    for block in document.blocks:
        if isinstance(block, GroupBlock):
            if block.label:
                parts.append(plain(block.label))
            parts.extend(
                _leaf_plain_text(child, markdown_lite=markdown_lite)
                for child in block.blocks
            )
        elif isinstance(block, AlternativeBlock):
            option = next(
                option for option in block.options if option.option_id == block.preferred_option_id
            )
            parts.extend(
                _leaf_plain_text(child, markdown_lite=markdown_lite)
                for child in option.blocks
            )
        else:
            parts.append(_leaf_plain_text(block, markdown_lite=markdown_lite))
    return "\n".join(part for part in parts if part)[:8_000]


def render_document_hash(document: RenderDocument) -> str:
    canonical = canonicalize_json_value(document.model_dump(mode="json")).canonical_bytes
    return sha256(canonical).hexdigest()
