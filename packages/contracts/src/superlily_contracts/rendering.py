"""Versioned contracts for deterministic chat-document rendering and delivery."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import canonicalize_json_value


RENDER_DOCUMENT_SCHEMA_VERSION = "1.0"
_CONVERSATION_KEY_RE = re.compile(r"^[a-z0-9_]+-(?:group|private)_[A-Za-z0-9.-]{1,256}$")
_FORBIDDEN_LATEX_RE = re.compile(
    r"\\(?:input|include|openin|openout|read|write|usepackage|documentclass|"
    r"newcommand|renewcommand|def|catcode|csname|endcsname|special|immediate|"
    r"write18|includegraphics|href|url)\b",
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


def _validate_mixed_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text contains a NUL byte")
    for kind, content in split_inline_math(value):
        if kind == "math" and (
            len(content) > 2_000 or _FORBIDDEN_LATEX_RE.search(content)
        ):
            raise ValueError("inline math contains a forbidden or oversized LaTeX expression")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TextBlock(_StrictModel):
    kind: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4_000)

    _validate_inline_math = field_validator("text")(_validate_mixed_text)


class HeadingBlock(_StrictModel):
    kind: Literal["heading"] = "heading"
    text: str = Field(min_length=1, max_length=240)
    level: Literal[1, 2] = 1

    _validate_inline_math = field_validator("text")(_validate_mixed_text)


class MathBlock(_StrictModel):
    kind: Literal["math"] = "math"
    latex: str = Field(min_length=1, max_length=2_000)
    display: bool = True

    @model_validator(mode="after")
    def reject_unsafe_commands(self) -> "MathBlock":
        if "\x00" in self.latex or _FORBIDDEN_LATEX_RE.search(self.latex):
            raise ValueError("math block contains a forbidden LaTeX command")
        return self


class ListBlock(_StrictModel):
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


RenderBlock = Annotated[
    TextBlock | HeadingBlock | MathBlock | ListBlock,
    Field(discriminator="kind"),
]


class RenderDocument(_StrictModel):
    schema_version: Literal["1.0"] = RENDER_DOCUMENT_SCHEMA_VERSION
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
        encoded = canonicalize_json_value(self.model_dump(mode="json")).canonical_bytes
        if len(encoded) > 32_768:
            raise ValueError("render document exceeds the canonical byte limit")
        return self


class RenderDocumentReceipt(_StrictModel):
    render_id: str = Field(min_length=36, max_length=36)
    artifact_id: str = Field(min_length=36, max_length=36)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mime_type: Literal["image/png"] = "image/png"
    byte_size: int = Field(ge=1, le=8_388_608)
    width_pixels: int = Field(ge=1, le=4_096)
    height_pixels: int = Field(ge=1, le=4_096)
    render_duration_ms: int = Field(ge=0, le=120_000)
    content_path: str
    duplicate: bool = False


class DeliveryAttemptIn(_StrictModel):
    instance_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    outcome: Literal["succeeded", "failed", "ambiguous"]
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=512)
    safe_error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")

    @model_validator(mode="after")
    def validate_outcome_fields(self) -> "DeliveryAttemptIn":
        if self.outcome == "succeeded" and (
            not self.platform_message_id or self.safe_error_code is not None
        ):
            raise ValueError("successful delivery requires only platform_message_id")
        if self.outcome != "succeeded" and not self.safe_error_code:
            raise ValueError("non-successful delivery requires safe_error_code")
        return self


def render_document_hash(document: RenderDocument) -> str:
    canonical = canonicalize_json_value(document.model_dump(mode="json")).canonical_bytes
    return sha256(canonical).hexdigest()
