"""Contracts for the first user-facing, bounded Phase 5 Agent slice."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AgentDispatchIn(_StrictModel):
    """Internal Core -> model-provider wake-up; it carries no prompt data."""

    schema_version: Literal["1.0"] = "1.0"
    target_type: Literal["run", "tool_loop"]
    target_id: str = Field(min_length=36, max_length=36)


class AgentTextDeliveryLeaseIn(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    instance_id: str = Field(min_length=1, max_length=128)


class AgentTextDeliveryLeaseOut(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    intent_id: str = Field(min_length=36, max_length=36)
    interaction_id: str = Field(min_length=36, max_length=36)
    instance_id: str = Field(min_length=1, max_length=128)
    conversation_key: str = Field(min_length=5, max_length=512)
    conversation_type: Literal["group", "private"]
    conversation_id: str = Field(min_length=1, max_length=256)
    reply_to_platform_message_id: str | None = Field(default=None, max_length=512)
    text: str = Field(min_length=1, max_length=8_192)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fence: int = Field(ge=1)
    lease_token: str = Field(min_length=32, max_length=128)
    lease_expires_at: str


class AgentTextDeliveryCompleteIn(_StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    instance_id: str = Field(min_length=1, max_length=128)
    fence: int = Field(ge=1)
    lease_token: str = Field(min_length=32, max_length=128)
    outcome: Literal["succeeded", "failed", "ambiguous"]
    platform_message_id: str | None = Field(default=None, max_length=512)
    safe_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
