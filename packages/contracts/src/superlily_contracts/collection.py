"""Authority-neutral C0-D collection reliability contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .models import ConversationRef, WireModel

COLLECTION_SCHEMA_VERSION = "1.0"

CaptureProfileName = Literal["off", "operational", "archive_full"]
CaptureStatus = Literal["unassessed", "complete", "partial", "unavailable"]
ActionOperation = Literal["add", "remove", "update", "observed_state", "unknown"]


class IngressRecordRef(WireModel):
    """Stable identity of one record in a bridge-owned durable spool."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    spool_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
    sequence: int = Field(ge=1)
    record_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    captured_at: AwareDatetime

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("captured_at must include a timezone")
        return value


class CaptureEnvelope(WireModel):
    """Completeness evidence reported by a collector, not retention authority."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    status: CaptureStatus = "unassessed"
    sanitizer_version: str | None = Field(default=None, max_length=64)
    original_payload_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    original_payload_size_bytes: int | None = Field(default=None, ge=0)
    omitted_fields: list[str] = Field(default_factory=list, max_length=256)
    platform_extra: dict[str, Any] = Field(default_factory=dict, max_length=256)
    reason: str | None = Field(default=None, max_length=4_096)

    @field_validator("omitted_fields")
    @classmethod
    def normalize_omitted_fields(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item or len(item) > 512 for item in normalized):
            raise ValueError("omitted field paths must be non-empty and at most 512 characters")
        if len(normalized) != len(set(normalized)):
            raise ValueError("omitted field paths must be unique")
        return sorted(normalized)

    @model_validator(mode="after")
    def require_incomplete_reason_and_sanitizer(self) -> "CaptureEnvelope":
        if self.status in {"partial", "unavailable"} and not self.reason:
            raise ValueError("partial or unavailable capture requires a reason")
        if self.platform_extra and not self.sanitizer_version:
            raise ValueError("platform_extra requires sanitizer_version")
        return self


class PlatformActionDetail(WireModel):
    """One factual platform action carried by an observed event."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    action_kind: str = Field(min_length=1, max_length=64)
    operation: ActionOperation = "unknown"
    actor_principal_id: str | None = Field(default=None, max_length=256)
    subject_principal_id: str | None = Field(default=None, max_length=256)
    target_source_event_id: str | None = Field(default=None, max_length=512)
    target_platform_message_id: str | None = Field(default=None, max_length=512)
    target_conversation: ConversationRef | None = None
    value: dict[str, Any] = Field(default_factory=dict, max_length=128)
    capture_status: CaptureStatus = "complete"
    occurred_at: AwareDatetime | None = None
    reason: str | None = Field(default=None, max_length=4_096)

    @model_validator(mode="after")
    def require_target_and_incomplete_reason(self) -> "PlatformActionDetail":
        if not (
            self.target_source_event_id
            or self.target_platform_message_id
            or self.subject_principal_id
        ):
            raise ValueError(
                "platform action requires a target event, platform message, or subject"
            )
        if self.capture_status in {"partial", "unavailable"} and not self.reason:
            raise ValueError("partial or unavailable action capture requires a reason")
        return self


class ConversationCapturePolicy(WireModel):
    """Reviewed exact-conversation capture policy; bridge reports are not authority."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    platform: str = Field(min_length=1, max_length=64)
    conversation: ConversationRef
    profile: CaptureProfileName
    image_policy: Literal["metadata_only"] = "metadata_only"
    binary_policy: Literal["metadata_only", "object_store"] = "metadata_only"
    retention_class: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    source_commit: str | None = Field(default=None, min_length=40, max_length=64)


class IngestReceipt(WireModel):
    """Durable Core acknowledgement returned after the database commit."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    receipt_id: str = Field(min_length=1, max_length=36)
    observation_id: str = Field(min_length=1, max_length=36)
    source_event_id: str = Field(min_length=1, max_length=512)
    instance_id: str = Field(min_length=1, max_length=128)
    outcome: Literal["committed", "duplicate"]
    duplicate: bool = False
    spool_id: str | None = Field(default=None, max_length=128)
    sequence: int | None = Field(default=None, ge=1)
    record_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    committed_at: AwareDatetime
    highest_contiguous_sequence: int | None = Field(default=None, ge=0)
    highest_seen_sequence: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_complete_spool_binding(self) -> "IngestReceipt":
        if self.duplicate != (self.outcome == "duplicate"):
            raise ValueError("receipt outcome and duplicate flag must agree")
        binding = (self.spool_id, self.sequence, self.record_sha256)
        if any(value is not None for value in binding) and not all(
            value is not None for value in binding
        ):
            raise ValueError("receipt spool binding must be entirely present or absent")
        if self.spool_id is None and (
            self.highest_contiguous_sequence is not None or self.highest_seen_sequence is not None
        ):
            raise ValueError("receipt without a spool cannot carry a watermark")
        return self


class CollectorWatermarkView(WireModel):
    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    instance_id: str = Field(min_length=1, max_length=128)
    spool_id: str = Field(min_length=1, max_length=128)
    highest_contiguous_sequence: int = Field(ge=0)
    highest_seen_sequence: int = Field(ge=0)
    next_gap_sequence: int | None = Field(default=None, ge=1)
    last_receipt_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def validate_watermark_order(self) -> "CollectorWatermarkView":
        if self.highest_contiguous_sequence > self.highest_seen_sequence:
            raise ValueError("contiguous watermark cannot exceed highest seen sequence")
        expected_gap = (
            self.highest_contiguous_sequence + 1
            if self.highest_seen_sequence > self.highest_contiguous_sequence
            else None
        )
        if self.next_gap_sequence != expected_gap:
            raise ValueError("next_gap_sequence must describe the first possible gap")
        return self


class IngressSpoolStatus(WireModel):
    """Typed bridge heartbeat snapshot for one local durable ingress spool."""

    schema_version: Literal["1.0"] = COLLECTION_SCHEMA_VERSION
    enabled: bool = True
    state: Literal["healthy", "pending", "quota_pressure", "quarantined", "error"]
    durability_mode: Literal["sqlite_full"]
    spool_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    pending_records: int = Field(ge=0)
    pending_bytes: int = Field(ge=0)
    committed_records: int = Field(ge=0)
    quarantined_records: int = Field(ge=0)
    quarantined_files: int = Field(ge=0)
    oldest_pending_seconds: float | None = Field(default=None, ge=0)
    live_bytes: int = Field(ge=0)
    quota_bytes: int = Field(ge=1)
    highest_sequence: int = Field(ge=0)
    replay_successes: int = Field(ge=0)
    replay_failures: int = Field(ge=0)
    capture_failures: int = Field(ge=0)
    quota_rejections: int = Field(ge=0)
    last_error: str | None = Field(default=None, max_length=4_096)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def validate_spool_state(self) -> "IngressSpoolStatus":
        if self.state != "error" and self.spool_id is None:
            raise ValueError("ready ingress spool status requires spool_id")
        if self.pending_records == 0 and self.oldest_pending_seconds is not None:
            raise ValueError("empty ingress spool cannot have oldest pending age")
        if self.pending_records > 0 and self.oldest_pending_seconds is None:
            raise ValueError("pending ingress spool requires oldest pending age")
        return self
