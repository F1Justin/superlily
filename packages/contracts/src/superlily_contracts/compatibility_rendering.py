"""Deterministic Phase 4 adapters from reviewed command results to RenderDocument."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .rendering import RenderDocument


class CompatibilityRenderingError(ValueError):
    def __init__(self, code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


class _CompatibilityModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        frozen=True,
    )


class ToolResultRenderIn(_CompatibilityModel):
    schema_version: Literal["1.0"] = "1.0"
    instance_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    conversation_key: str = Field(min_length=1, max_length=320)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=512)


class HelpCommandEntry(_CompatibilityModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_./:-]+$")
    summary: str = Field(min_length=1, max_length=240)
    usage: str | None = Field(default=None, min_length=1, max_length=240)

    @field_validator("summary", "usage")
    @classmethod
    def reject_control_text(cls, value: str | None) -> str | None:
        if value is not None and (
            "\x00" in value
            or any(ord(character) == 127 for character in value)
        ):
            raise ValueError("help text contains a forbidden control character")
        return value


class HelpDocumentIn(_CompatibilityModel):
    schema_version: Literal["1.0"] = "1.0"
    instance_id: str = Field(
        min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"
    )
    conversation_key: str = Field(min_length=1, max_length=320)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    title: str = Field(default="莉莉帮助", min_length=1, max_length=120)
    commands: list[HelpCommandEntry] = Field(min_length=1, max_length=64)

    @field_validator("commands")
    @classmethod
    def command_names_are_unique(
        cls, value: list[HelpCommandEntry]
    ) -> list[HelpCommandEntry]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("help command names must be unique")
        return value


def _base_document(
    request: ToolResultRenderIn,
    *,
    title: str,
    blocks: list[dict[str, Any]],
) -> RenderDocument:
    return RenderDocument(
        schema_version="1.3",
        instance_id=request.instance_id,
        conversation_key=request.conversation_key,
        source_event_id=request.source_event_id,
        title=title,
        blocks=blocks,
    )


def render_tool_result_document(
    request: ToolResultRenderIn,
    *,
    tool_id: str,
    descriptor_version: str,
    tool_input: Any,
    output: Any,
) -> RenderDocument:
    """Convert only the explicitly migrated tool versions."""

    if tool_id == "status.inspect" and descriptor_version == "1.0.2":
        if not isinstance(output, dict):
            raise CompatibilityRenderingError(
                "invalid_tool_result", "status result must be an object"
            )
        card_status = {
            "ok": "success",
            "degraded": "warning",
            "unavailable": "error",
        }.get(output.get("status"))
        if card_status is None:
            raise CompatibilityRenderingError(
                "invalid_tool_result", "status result has an invalid state"
            )
        return _base_document(
            request,
            title="莉莉状态",
            blocks=[
                {
                    "kind": "card",
                    "node_id": "status-card",
                    "status": card_status,
                    "title": str(output["status"]).upper(),
                    "body": f"状态提供者 {output['provider_id']} 已完成受控检查。",
                    "fields": [
                        {"label": "检查时间", "value": str(output["checked_at"])},
                        {"label": "检查范围", "value": str(output["scope"])},
                        {
                            "label": "描述符",
                            "value": str(output["descriptor_hash"])[:16],
                        },
                        {
                            "label": "实现",
                            "value": str(output["implementation_hash"])[:16],
                        },
                    ],
                },
            ],
        )

    if tool_id == "wolfram.run" and descriptor_version == "1.0.0":
        if (
            not isinstance(output, dict)
            or output.get("kind") != "text"
            or not isinstance(output.get("text"), str)
        ):
            raise CompatibilityRenderingError(
                "invalid_tool_result", "Wolfram result must be bounded text"
            )
        return _base_document(
            request,
            title="Wolfram 计算结果",
            blocks=[
                {
                    "kind": "code",
                    "node_id": "wolfram-result",
                    "language": "wolfram",
                    "code": output["text"],
                    "wrap": True,
                }
            ],
        )

    if tool_id == "latex.render" and descriptor_version == "1.0.0":
        if (
            not isinstance(output, dict)
            or output.get("kind") != "image"
            or output.get("mime_type") != "image/png"
            or not isinstance(output.get("artifact_id"), str)
        ):
            raise CompatibilityRenderingError(
                "invalid_tool_result", "LaTeX result must reference one PNG artifact"
            )
        latex = (
            tool_input.get("latex")
            if isinstance(tool_input, dict) and isinstance(tool_input.get("latex"), str)
            else "公式"
        )
        accessibility = f"LaTeX 公式：{latex}"[:2_000]
        return _base_document(
            request,
            title="LaTeX 公式",
            blocks=[
                {
                    "kind": "image",
                    "node_id": "latex-result",
                    "artifact_id": output["artifact_id"],
                    "caption": "LaTeX 渲染结果",
                    "accessibility_text": accessibility,
                }
            ],
        )

    raise CompatibilityRenderingError(
        "tool_result_not_migrated",
        "tool result does not have a reviewed Phase 4 renderer",
    )


def render_help_document(payload: HelpDocumentIn) -> RenderDocument:
    items = [
        (
            f"**{item.name}** — {item.summary}"
            + (f"（用法：{item.usage}）" if item.usage else "")
        )
        for item in payload.commands
    ]
    return RenderDocument(
        schema_version="1.3",
        instance_id=payload.instance_id,
        conversation_key=payload.conversation_key,
        source_event_id=payload.source_event_id,
        title=payload.title,
        blocks=[
            {
                "kind": "list",
                "node_id": "help-commands",
                "items": items,
            }
        ],
    )
