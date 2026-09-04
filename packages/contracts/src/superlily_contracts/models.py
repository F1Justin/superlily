"""Wire models for the v1 ingestion API.

The contracts distinguish a platform event from an observation of that event.
Two QQ accounts can observe the same source event without producing the same
observation or idempotency key.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .sanitization import replace_nul

API_SCHEMA_VERSION = "1.0"

PlatformCapabilityName = Literal[
    "send_text",
    "send_image",
    "send_file",
    "send_voice",
    "send_video",
    "reply",
    "mention",
    "recall_own",
    "markdown",
    "buttons",
    "reactions",
    "edit",
    "thread",
    "ephemeral",
]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def remove_postgres_incompatible_nul(cls, value: Any) -> Any:
        return replace_nul(value)


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
    account_name: str | None = Field(default=None, max_length=512)
    display_name: str | None = Field(default=None, max_length=512)
    name: str | None = Field(default=None, max_length=512)
    title: str | None = Field(default=None, max_length=512)
    level: str | None = Field(default=None, max_length=512)
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
    ingress: "IngressRecordRef | None" = None
    capture: "CaptureEnvelope | None" = None
    actions: list["PlatformActionDetail"] = Field(default_factory=list, max_length=128)
    occurred_at: AwareDatetime
    raw: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value


from .collection import (  # noqa: E402
    CaptureEnvelope,
    IngressRecordRef,
    IngressSpoolStatus,
    PlatformActionDetail,
)

EventIn.model_rebuild()


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


class PlatformCapabilities(WireModel):
    profile: str = Field(min_length=1, max_length=128)
    supported: list[PlatformCapabilityName] = Field(default_factory=list, max_length=64)
    limits: dict[str, int] = Field(default_factory=dict, max_length=64)

    @field_validator("supported")
    @classmethod
    def normalize_supported(
        cls,
        value: list[PlatformCapabilityName],
    ) -> list[PlatformCapabilityName]:
        if len(value) != len(set(value)):
            raise ValueError("supported capabilities must be unique")
        return sorted(value)

    @field_validator("limits")
    @classmethod
    def validate_limits(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not key.strip() or len(key) > 128 for key in value):
            raise ValueError("capability limit names must be non-empty and at most 128 characters")
        if any(limit < 0 for limit in value.values()):
            raise ValueError("capability limits must be non-negative")
        return dict(sorted(value.items()))


class HeartbeatIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    instance: BotInstanceRef
    process_status: Literal["starting", "running", "stopping", "stopped", "error", "unknown"]
    connection_status: Literal["connected", "disconnected", "degraded", "unknown"]
    last_event_at: AwareDatetime | None = None
    error_summary: str | None = Field(default=None, max_length=4096)
    occurred_at: AwareDatetime
    capabilities: PlatformCapabilities | None = None
    ingress_spool: IngressSpoolStatus | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class QQGroupProfileSnapshot(WireModel):
    group_id: str = Field(min_length=1, max_length=256)
    group_name: str | None = Field(default=None, max_length=512)
    group_remark: str | None = Field(default=None, max_length=512)
    member_count: int | None = Field(default=None, ge=0)
    max_member_count: int | None = Field(default=None, ge=0)
    whole_group_ban: bool | None = None


class QQGroupMemberSnapshot(WireModel):
    user_id: str = Field(min_length=1, max_length=256)
    nickname: str | None = Field(default=None, max_length=512)
    card: str | None = Field(default=None, max_length=512)
    role: Literal["owner", "admin", "member", "unknown"] = "unknown"
    title: str | None = Field(default=None, max_length=512)
    member_level: str | None = Field(default=None, max_length=512)
    qq_level: int | None = Field(default=None, ge=0)
    joined_at: AwareDatetime | None = None
    last_sent_at: AwareDatetime | None = None
    muted_until: AwareDatetime | None = None
    is_robot: bool | None = None


class QQFriendSnapshot(WireModel):
    user_id: str = Field(min_length=1, max_length=256)
    nickname: str | None = Field(default=None, max_length=512)
    remark: str | None = Field(default=None, max_length=512)
    category_id: str | None = Field(default=None, max_length=256)
    category_name: str | None = Field(default=None, max_length=512)


class QQDirectorySnapshotIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    instance: BotInstanceRef
    snapshot_kind: Literal["group", "friends"]
    observed_at: AwareDatetime
    source_apis: list[str] = Field(min_length=1, max_length=8)
    capture_status: Literal["complete", "partial"]
    reason: str | None = Field(default=None, max_length=4096)
    group: QQGroupProfileSnapshot | None = None
    members: list[QQGroupMemberSnapshot] = Field(default_factory=list, max_length=10_000)
    friends: list[QQFriendSnapshot] = Field(default_factory=list, max_length=100_000)

    @model_validator(mode="after")
    def require_kind_payload(self) -> "QQDirectorySnapshotIn":
        if self.snapshot_kind == "group":
            if self.group is None or self.friends:
                raise ValueError("group snapshot requires group and must not contain friends")
            return self
        if self.group is not None or self.members:
            raise ValueError("friends snapshot must not contain group or members")
        return self


class RuntimePlugin(WireModel):
    plugin_id: str = Field(min_length=1, max_length=256)
    module_name: str = Field(min_length=1, max_length=512)
    display_name: str | None = Field(default=None, max_length=512)
    matcher_count: int = Field(default=0, ge=0, le=100_000)
    classified_matcher_count: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def classified_count_cannot_exceed_total(self) -> "RuntimePlugin":
        if self.classified_matcher_count > self.matcher_count:
            raise ValueError("classified_matcher_count cannot exceed matcher_count")
        return self


class RuntimeCommandCandidate(WireModel):
    plugin_id: str = Field(min_length=1, max_length=256)
    module_name: str = Field(min_length=1, max_length=512)
    matcher_type: str = Field(min_length=1, max_length=128)
    kind: Literal["command", "token", "prefix", "exact", "regex", "suffix", "contains"]
    triggers: list[str] = Field(min_length=1, max_length=256)
    priority: int | None = None
    block: bool | None = None
    ignore_case: bool | None = None
    regex_flags: int | None = Field(default=None, ge=0, le=2_147_483_647)
    complete: bool
    rule_checker_count: int = Field(ge=0, le=1_024)
    unknown_rule_checkers: list[str] = Field(max_length=64)
    permission_checker_count: int = Field(ge=0, le=1_024)

    @field_validator("triggers")
    @classmethod
    def validate_triggers(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2_048 for item in value):
            raise ValueError("runtime command triggers must be non-empty and at most 2048 characters")
        return value

    @field_validator("unknown_rule_checkers")
    @classmethod
    def validate_checker_labels(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 512 for item in value):
            raise ValueError("runtime checker labels must be non-empty and at most 512 characters")
        return value


class CommandRegistrySnapshotIn(WireModel):
    schema_version: Literal["1.0"] = API_SCHEMA_VERSION
    instance: BotInstanceRef
    snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    observed_at: AwareDatetime
    plugins: list[RuntimePlugin] = Field(default_factory=list, max_length=2_048)
    candidates: list[RuntimeCommandCandidate] = Field(default_factory=list, max_length=4_096)
