"""Wire models for the v1 ingestion API.

The contracts distinguish a platform event from an observation of that event.
Two QQ accounts can observe the same source event without producing the same
observation or idempotency key.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

API_SCHEMA_VERSION = "1.0"


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BotInstanceRef(WireModel):
    instance_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)
    adapter: str = Field(min_length=1, max_length=64)
    bot_id: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=128)


class ConversationRef(WireModel):
    id: str = Field(min_length=1, max_length=256)
    type: Literal["group", "private", "channel", "system", "unknown"]
    name: str | None = Field(default=None, max_length=512)


class SenderRef(WireModel):
    id: str = Field(min_length=1, max_length=256)
    name: str | None = Field(default=None, max_length=512)
    roles: list[str] = Field(default_factory=list, max_length=32)


class Attachment(WireModel):
    type: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, max_length=512)
    media_type: str | None = Field(default=None, max_length=256)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    platform_id: str | None = Field(default=None, max_length=512)


class MessageRef(WireModel):
    id: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=200_000)
    segments: list[dict[str, Any]] = Field(default_factory=list, max_length=1024)
    attachments: list[Attachment] = Field(default_factory=list, max_length=128)


class EventReference(WireModel):
    type: Literal["reply_to", "quote_of", "forward_of", "mentions", "derived_from"]
    source_event_id: str | None = Field(default=None, max_length=512)
    platform_message_id: str | None = Field(default=None, max_length=512)
    conversation_id: str | None = Field(default=None, max_length=256)
    conversation_type: Literal["group", "private", "channel", "system", "unknown"] | None = None
    sender_id: str | None = Field(default=None, max_length=256)
    raw: dict[str, Any] | None = None

    @model_validator(mode="after")
    def require_target_hint(self) -> "EventReference":
        if not (self.source_event_id or self.platform_message_id or self.sender_id):
            raise ValueError("reference requires source_event_id, platform_message_id, or sender_id")
        return self


class EventIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    source_event_id: str = Field(min_length=1, max_length=512)
    instance: BotInstanceRef
    event_type: str = Field(min_length=1, max_length=128)
    conversation: ConversationRef
    sender: SenderRef | None = None
    message: MessageRef | None = None
    references: list[EventReference] = Field(default_factory=list, max_length=128)
    occurred_at: AwareDatetime
    raw: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


class ResponseIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    source_response_id: str = Field(min_length=1, max_length=512)
    instance: BotInstanceRef
    trigger_observation_id: str | None = Field(default=None, max_length=64)
    trigger_source_event_id: str | None = Field(default=None, max_length=512)
    trace_id: str | None = Field(default=None, max_length=128)
    response_type: str = Field(min_length=1, max_length=128)
    conversation: ConversationRef
    platform_message_id: str | None = Field(default=None, max_length=512)
    reply_to_platform_message_id: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=200_000)
    segments: list[dict[str, Any]] = Field(default_factory=list, max_length=1024)
    attachments: list[Attachment] = Field(default_factory=list, max_length=128)
    success: bool
    error: str | None = Field(default=None, max_length=16_384)
    latency_ms: int | None = Field(default=None, ge=0)
    occurred_at: AwareDatetime
    raw: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HeartbeatIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    instance: BotInstanceRef
    process_status: Literal["starting", "running", "stopping", "stopped", "error", "unknown"]
    connection_status: Literal["connected", "disconnected", "degraded", "unknown"]
    last_event_at: AwareDatetime | None = None
    error_summary: str | None = Field(default=None, max_length=4096)
    occurred_at: AwareDatetime
    metadata: dict[str, Any] = Field(default_factory=dict)
