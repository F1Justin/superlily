from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    JSON,
    Boolean,
    CheckConstraint,
    DDL,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy import text as sql_text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class BotInstance(Base):
    __tablename__ = "bot_instances"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256))
    version: Mapped[str | None] = mapped_column(String(128))
    reported_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SourceEvent(Base):
    __tablename__ = "source_events"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    conversation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(512))
    correlation_fingerprint: Mapped[str | None] = mapped_column(String(64))
    correlation_version: Mapped[str | None] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_source_events_occurred_at", "occurred_at"),
        Index("ix_source_events_correlation_time", "correlation_fingerprint", "occurred_at"),
    )


class EventObservation(Base):
    __tablename__ = "event_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False)
    reported_source_event_id: Mapped[str] = mapped_column(String(512), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(512))
    instance_id: Mapped[str] = mapped_column(ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_name: Mapped[str | None] = mapped_column(String(512))
    sender_id: Mapped[str | None] = mapped_column(String(256))
    sender_name: Mapped[str | None] = mapped_column(String(512))
    sender_roles_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    text: Mapped[str | None] = mapped_column(Text)
    segments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    attachments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capture_profile: Mapped[str] = mapped_column(String(32), default="operational", nullable=False)
    capture_policy_version: Mapped[str] = mapped_column(
        String(64), default="default-operational-v1", nullable=False
    )
    capture_status: Mapped[str] = mapped_column(String(32), default="unassessed", nullable=False)
    sanitizer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    collector_sanitizer_version: Mapped[str | None] = mapped_column(String(64))
    original_payload_sha256: Mapped[str | None] = mapped_column(String(64))
    original_payload_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    omitted_fields_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    platform_extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capture_reason: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", "idempotency_key", name="uq_event_observation_idempotency"),
        UniqueConstraint(
            "instance_id",
            "reported_source_event_id",
            name="uq_event_observation_reported_source",
        ),
        Index("ix_event_observations_received_at", "received_at"),
        Index("ix_event_observations_source_event_id", "source_event_id"),
        Index(
            "ix_event_observations_instance_message",
            "instance_id",
            "platform_message_id",
        ),
        CheckConstraint(
            "capture_profile IN ('off', 'operational', 'archive_full')",
            name="ck_event_observation_capture_profile",
        ),
        CheckConstraint(
            "capture_status IN ('unassessed', 'complete', 'partial', 'unavailable')",
            name="ck_event_observation_capture_status",
        ),
        CheckConstraint(
            "original_payload_size_bytes IS NULL OR original_payload_size_bytes >= 0",
            name="ck_event_observation_payload_size",
        ),
    )


class ConversationCaptureProfile(Base):
    __tablename__ = "conversation_capture_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    capture_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    image_policy: Mapped[str] = mapped_column(String(32), default="metadata_only", nullable=False)
    binary_policy: Mapped[str] = mapped_column(String(32), default="metadata_only", nullable=False)
    retention_class: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_commit: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "platform",
            "conversation_type",
            "conversation_id",
            name="uq_conversation_capture_profile_scope",
        ),
        CheckConstraint(
            "capture_profile IN ('off', 'operational', 'archive_full')",
            name="ck_conversation_capture_profile",
        ),
        CheckConstraint(
            "image_policy IN ('metadata_only')",
            name="ck_conversation_capture_image_policy",
        ),
        CheckConstraint(
            "binary_policy IN ('metadata_only', 'object_store')",
            name="ck_conversation_capture_binary_policy",
        ),
        Index("ix_conversation_capture_profiles_active", "active"),
    )


class PlatformActionObservation(Base):
    __tablename__ = "platform_action_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("event_observations.id", ondelete="CASCADE"), nullable=False
    )
    observer_instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    action_index: Mapped[int] = mapped_column(Integer, nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_principal_id: Mapped[str | None] = mapped_column(String(256))
    subject_principal_id: Mapped[str | None] = mapped_column(String(256))
    target_reported_source_event_id: Mapped[str | None] = mapped_column(String(512))
    target_platform_message_id: Mapped[str | None] = mapped_column(String(512))
    target_conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    target_conversation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_source_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("source_events.id", ondelete="SET NULL")
    )
    resolver_status: Mapped[str] = mapped_column(String(32), default="unresolved", nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capture_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "observation_id", "action_index", name="uq_platform_action_observation_index"
        ),
        CheckConstraint(
            "operation IN ('add', 'remove', 'update', 'observed_state', 'unknown')",
            name="ck_platform_action_operation",
        ),
        CheckConstraint(
            "capture_status IN ('unassessed', 'complete', 'partial', 'unavailable')",
            name="ck_platform_action_capture_status",
        ),
        CheckConstraint(
            "resolver_status IN ('resolved', 'unresolved', 'ambiguous', 'unavailable')",
            name="ck_platform_action_resolver_status",
        ),
        CheckConstraint(
            "target_reported_source_event_id IS NOT NULL "
            "OR target_platform_message_id IS NOT NULL "
            "OR subject_principal_id IS NOT NULL",
            name="ck_platform_action_target_hint",
        ),
        Index("ix_platform_actions_observation", "observation_id"),
        Index("ix_platform_actions_target_source", "target_source_event_id"),
        Index(
            "ix_platform_actions_pending_target",
            "observer_instance_id",
            "target_conversation_type",
            "target_conversation_id",
            "target_platform_message_id",
        ),
    )


class IngressReceiptRecord(Base):
    __tablename__ = "ingress_receipts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    observation_id: Mapped[str] = mapped_column(
        ForeignKey("event_observations.id", ondelete="CASCADE"), nullable=False
    )
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    spool_id: Mapped[str | None] = mapped_column(String(128))
    collector_sequence: Mapped[int | None] = mapped_column(BigInteger)
    record_sha256: Mapped[str | None] = mapped_column(String(64))
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("observation_id", name="uq_ingress_receipt_observation"),
        UniqueConstraint(
            "instance_id",
            "spool_id",
            "collector_sequence",
            name="uq_ingress_receipt_spool_sequence",
        ),
        CheckConstraint(
            "(spool_id IS NULL AND collector_sequence IS NULL AND record_sha256 IS NULL "
            "AND captured_at IS NULL) OR (spool_id IS NOT NULL AND collector_sequence IS NOT NULL "
            "AND record_sha256 IS NOT NULL AND captured_at IS NOT NULL)",
            name="ck_ingress_receipt_spool_binding",
        ),
        CheckConstraint(
            "collector_sequence IS NULL OR collector_sequence >= 1",
            name="ck_ingress_receipt_sequence",
        ),
        Index("ix_ingress_receipts_instance_committed", "instance_id", "committed_at"),
    )


class CollectorWatermark(Base):
    __tablename__ = "collector_watermarks"

    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="CASCADE"), primary_key=True
    )
    spool_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    highest_contiguous_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    highest_seen_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_receipt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "highest_contiguous_sequence >= 0 "
            "AND highest_seen_sequence >= highest_contiguous_sequence",
            name="ck_collector_watermark_order",
        ),
        Index("ix_collector_watermarks_updated", "updated_at"),
    )


class EventLink(Base):
    __tablename__ = "event_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    from_source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False)
    from_observation_id: Mapped[str] = mapped_column(ForeignKey("event_observations.id", ondelete="CASCADE"), nullable=False)
    to_source_event_id: Mapped[str | None] = mapped_column(ForeignKey("source_events.id", ondelete="SET NULL"))
    relation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_source_event_id: Mapped[str | None] = mapped_column(String(512))
    target_platform_message_id: Mapped[str | None] = mapped_column(String(512))
    target_conversation_id: Mapped[str | None] = mapped_column(String(256))
    target_conversation_type: Mapped[str | None] = mapped_column(String(32))
    target_sender_id: Mapped[str | None] = mapped_column(String(256))
    confidence: Mapped[int | None] = mapped_column(Integer)
    resolver_status: Mapped[str] = mapped_column(String(32), default="unresolved", nullable=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_event_links_from_source", "from_source_event_id"),
        Index("ix_event_links_from_observation", "from_observation_id"),
        Index("ix_event_links_to_source", "to_source_event_id"),
        Index("ix_event_links_resolver_status", "resolver_status"),
        Index(
            "ix_event_links_pending_target",
            "resolver_status",
            "target_platform_message_id",
            "target_conversation_id",
            "target_conversation_type",
        ),
    )


class EventDecision(Base):
    __tablename__ = "event_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False)
    deciding_observation_id: Mapped[str | None] = mapped_column(
        ForeignKey("event_observations.id", ondelete="SET NULL")
    )
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_instance_id: Mapped[str | None] = mapped_column(String(128))
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_event_id", name="uq_event_decisions_source_event"),
        Index("ix_event_decisions_created_at", "created_at"),
        Index("ix_event_decisions_updated_at", "updated_at"),
        Index("ix_event_decisions_decision_type", "decision_type"),
        Index("ix_event_decisions_target_instance", "target_instance_id"),
    )


class ResponseRecord(Base):
    __tablename__ = "responses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_response_id: Mapped[str] = mapped_column(String(512), nullable=False)
    instance_id: Mapped[str] = mapped_column(ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    trigger_observation_id: Mapped[str | None] = mapped_column(ForeignKey("event_observations.id", ondelete="SET NULL"))
    trigger_source_event_id: Mapped[str | None] = mapped_column(String(512))
    trace_id: Mapped[str | None] = mapped_column(String(128))
    response_type: Mapped[str] = mapped_column(String(128), nullable=False)
    platform: Mapped[str] = mapped_column(String(64), nullable=False)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    conversation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(512))
    reply_to_platform_message_id: Mapped[str | None] = mapped_column(String(512))
    text: Mapped[str | None] = mapped_column(Text)
    segments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    attachments_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", "idempotency_key", name="uq_response_idempotency"),
        Index("ix_responses_received_at", "received_at"),
        Index("ix_responses_trigger_source", "trigger_source_event_id"),
        Index(
            "ix_responses_platform_message",
            "platform",
            "conversation_type",
            "platform_message_id",
        ),
    )


class InstanceStatusTransition(Base):
    __tablename__ = "instance_status_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(ForeignKey("bot_instances.id", ondelete="CASCADE"), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_status_transitions_instance_created", "instance_id", "created_at"),)


class CommandRegistrySnapshot(Base):
    __tablename__ = "command_registry_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(ForeignKey("bot_instances.id", ondelete="CASCADE"), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plugins_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    candidates_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", "snapshot_hash", name="uq_command_registry_snapshot_hash"),
        Index("ix_command_registry_snapshots_instance_received", "instance_id", "received_at"),
    )


class EventClaim(Base):
    __tablename__ = "event_claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False)
    instance_id: Mapped[str] = mapped_column(ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(ForeignKey("event_decisions.id", ondelete="SET NULL"))
    decision_revision: Mapped[int | None] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enforced: Mapped[bool] = mapped_column(Boolean, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        UniqueConstraint("instance_id", "idempotency_key", name="uq_event_claim_idempotency"),
        UniqueConstraint("source_event_id", "instance_id", name="uq_event_claim_source_instance"),
        Index(
            "uq_event_claim_enforced_allow_owner",
            "source_event_id",
            unique=True,
            postgresql_where=sql_text("enforced AND action = 'allow'"),
            sqlite_where=sql_text("enforced = 1 AND action = 'allow'"),
        ),
        Index("ix_event_claims_source_event", "source_event_id"),
        Index("ix_event_claims_created_at", "created_at"),
        Index("ix_event_claims_action", "action"),
    )


class ToolDescriptorRecord(Base):
    __tablename__ = "tool_descriptors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    source_plugin: Mapped[str] = mapped_column(String(512), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="reviewed", nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), default="reviewed", nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    descriptor_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    import_outcome: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tool_id", "version", name="uq_tool_descriptor_identity"),
        UniqueConstraint("descriptor_hash", name="uq_tool_descriptor_hash"),
        CheckConstraint("review_status IN ('reviewed')", name="ck_tool_descriptor_review_status"),
        CheckConstraint(
            "lifecycle IN ('draft', 'reviewed', 'active', 'suspended', 'retired', 'revoked')",
            name="ck_tool_descriptor_lifecycle",
        ),
        CheckConstraint("import_outcome IN ('accepted')", name="ck_tool_descriptor_import_outcome"),
        Index("ix_tool_descriptors_tool_id", "tool_id"),
        Index("ix_tool_descriptors_lifecycle", "lifecycle"),
    )


class ToolDescriptorLifecycleEvent(Base):
    __tablename__ = "tool_descriptor_lifecycle_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    descriptor_id: Mapped[str] = mapped_column(
        ForeignKey("tool_descriptors.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_lifecycle: Mapped[str | None] = mapped_column(String(32))
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("descriptor_id", "sequence", name="uq_tool_descriptor_lifecycle_sequence"),
        CheckConstraint(
            "lifecycle IN ('draft', 'reviewed', 'active', 'suspended', 'retired', 'revoked')",
            name="ck_tool_descriptor_event_lifecycle",
        ),
        Index("ix_tool_descriptor_lifecycle_created", "descriptor_id", "created_at"),
    )


class ToolProvider(Base):
    __tablename__ = "tool_providers"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(256), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    allowed_protocols_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    tool_selectors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "lifecycle IN ('registered', 'active', 'quarantined', 'retired', 'revoked')",
            name="ck_tool_provider_lifecycle",
        ),
        Index("ix_tool_providers_lifecycle", "lifecycle"),
    )


class ToolProviderCredential(Base):
    __tablename__ = "tool_provider_credentials"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source IN ('environment')", name="ck_tool_provider_credential_source"),
        CheckConstraint(
            "lifecycle IN ('active', 'revoked')", name="ck_tool_provider_credential_lifecycle"
        ),
        Index("ix_tool_provider_credentials_provider", "provider_id"),
    )


class ToolProviderLifecycleEvent(Base):
    __tablename__ = "tool_provider_lifecycle_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_lifecycle: Mapped[str | None] = mapped_column(String(32))
    lifecycle: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "sequence", name="uq_tool_provider_lifecycle_sequence"),
        CheckConstraint(
            "lifecycle IN ('registered', 'active', 'quarantined', 'retired', 'revoked')",
            name="ck_tool_provider_event_lifecycle",
        ),
        Index("ix_tool_provider_lifecycle_created", "provider_id", "created_at"),
    )


class ToolProviderInventorySnapshot(Base):
    __tablename__ = "tool_provider_inventory_snapshots"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True, nullable=False)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "idempotency_key", name="uq_tool_inventory_idempotency"),
        Index("ix_tool_inventory_provider_received", "provider_id", "received_at"),
        Index("ix_tool_inventory_provider_hash", "provider_id", "snapshot_hash"),
    )


class ToolProviderInventoryEntry(Base):
    __tablename__ = "tool_provider_inventory_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("tool_provider_inventory_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    descriptor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    implementation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_enforcement_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("snapshot_id", "tool_id", name="uq_tool_inventory_entry_tool"),
        Index("ix_tool_inventory_entries_tool", "tool_id"),
    )


class ToolProviderHeartbeat(Base):
    __tablename__ = "tool_provider_heartbeats"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), default=new_id, unique=True, nullable=False)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    health: Mapped[str] = mapped_column(String(32), nullable=False)
    current_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    oldest_work_age_ms: Mapped[int | None] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("provider_id", "observed_at", name="uq_tool_provider_heartbeat_observed"),
        CheckConstraint(
            "health IN ('starting', 'healthy', 'degraded', 'unavailable', 'unknown')",
            name="ck_tool_provider_heartbeat_health",
        ),
        CheckConstraint(
            "current_concurrency >= 0 AND current_concurrency <= max_concurrency",
            name="ck_tool_provider_heartbeat_capacity",
        ),
        Index("ix_tool_provider_heartbeat_received", "provider_id", "received_at"),
    )


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_id: Mapped[str] = mapped_column(
        ForeignKey("tool_descriptors.id", ondelete="RESTRICT"), nullable=False
    )
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    descriptor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    input_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    capability_snapshot_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_provider_id: Mapped[str | None] = mapped_column(String(128))
    execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    transition_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "creator_type",
            "creator_id",
            "idempotency_key",
            name="uq_tool_invocation_idempotency",
        ),
        CheckConstraint(
            "creator_type IN ('command', 'admin_api')",
            name="ck_tool_invocation_creator_type",
        ),
        CheckConstraint(
            "execution_mode IN ('off', 'ledger_only', 'canary', 'enforce')",
            name="ck_tool_invocation_execution_mode",
        ),
        CheckConstraint(
            "state IN ('proposed', 'rejected', 'recorded_only', "
            "'awaiting_confirmation', 'queued', 'leased', 'running', 'succeeded', "
            "'failed', 'timed_out', 'cancel_requested', 'cancelled', "
            "'unknown_completion', 'expired', 'lease_expired')",
            name="ck_tool_invocation_state",
        ),
        CheckConstraint("transition_sequence >= 1", name="ck_tool_invocation_sequence"),
        CheckConstraint(
            "((state IN ('rejected', 'recorded_only', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'unknown_completion', 'expired') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('rejected', 'recorded_only', 'succeeded', 'failed', 'timed_out', "
            "'cancelled', 'unknown_completion', 'expired') AND terminal_at IS NULL))",
            name="ck_tool_invocation_terminal_time",
        ),
        Index("ix_tool_invocations_tool", "tool_id", "descriptor_version"),
        Index(
            "ix_tool_invocations_provider_queue",
            "selected_provider_id",
            "state",
            "created_at",
        ),
        Index("ix_tool_invocations_state_deadline", "state", "deadline_at"),
        Index("ix_tool_invocations_created", "created_at"),
    )


class ToolInvocationTransition(Base):
    __tablename__ = "tool_invocation_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "invocation_id",
            "sequence",
            name="uq_tool_invocation_transition_sequence",
        ),
        CheckConstraint("sequence >= 1", name="ck_tool_invocation_transition_sequence"),
        CheckConstraint(
            "event IN ('propose', 'reject', 'record_only', 'require_confirmation', "
            "'confirm', 'confirmation_expire', 'queue', 'lease', 'start', "
            "'complete_success', 'complete_failure', 'request_cancel', 'cancel', "
            "'lease_expire', 'timeout', 'unknown_completion', 'requeue')",
            name="ck_tool_invocation_transition_event",
        ),
        CheckConstraint(
            "state IN ('proposed', 'rejected', 'recorded_only', "
            "'awaiting_confirmation', 'queued', 'leased', 'running', 'succeeded', "
            "'failed', 'timed_out', 'cancel_requested', 'cancelled', "
            "'unknown_completion', 'expired', 'lease_expired')",
            name="ck_tool_invocation_transition_state",
        ),
        CheckConstraint(
            "actor_type IN ('command', 'admin_api', 'provider', 'reaper', 'system')",
            name="ck_tool_invocation_transition_actor",
        ),
        CheckConstraint(
            "((event = 'propose' AND previous_state IS NULL AND state = 'proposed') OR "
            "(event <> 'propose' AND previous_state IS NOT NULL))",
            name="ck_tool_invocation_transition_initial",
        ),
        Index(
            "ix_tool_invocation_transitions_created",
            "invocation_id",
            "created_at",
        ),
    )


class ToolAttempt(Base):
    __tablename__ = "tool_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="RESTRICT"), nullable=False
    )
    inventory_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    implementation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lease_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    permissions_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    permissions_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    usage_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[Any | None] = mapped_column(JSON)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    provider_result_id: Mapped[str | None] = mapped_column(String(512))
    error_code: Mapped[str | None] = mapped_column(String(64))
    safe_error_detail: Mapped[str | None] = mapped_column(String(512))
    event_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "invocation_id",
            "attempt_number",
            name="uq_tool_attempt_number",
        ),
        UniqueConstraint(
            "invocation_id",
            "fencing_token",
            name="uq_tool_attempt_fencing_token",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_tool_attempt_number"),
        CheckConstraint("fencing_token >= 1", name="ck_tool_attempt_fencing_token"),
        CheckConstraint(
            "event_sequence >= 1",
            name="ck_tool_attempt_current_event_sequence",
        ),
        CheckConstraint(
            "state IN ('leased', 'running', 'succeeded', 'failed', 'cancelled', "
            "'lease_expired', 'unknown_completion')",
            name="ck_tool_attempt_state",
        ),
        CheckConstraint(
            "((state IN ('succeeded', 'failed', 'cancelled', 'lease_expired', "
            "'unknown_completion') AND completed_at IS NOT NULL) OR "
            "(state IN ('leased', 'running') AND completed_at IS NULL))",
            name="ck_tool_attempt_terminal_time",
        ),
        Index(
            "uq_tool_attempt_active_invocation",
            "invocation_id",
            unique=True,
            postgresql_where=sql_text("state IN ('leased', 'running')"),
            sqlite_where=sql_text("state IN ('leased', 'running')"),
        ),
        Index("ix_tool_attempt_provider_state", "provider_id", "state"),
        Index("ix_tool_attempt_lease_expiry", "state", "lease_expires_at"),
    )


class ToolAttemptEvent(Base):
    __tablename__ = "tool_attempt_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("tool_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="uq_tool_attempt_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_tool_attempt_event_sequence"),
        CheckConstraint("fencing_token >= 1", name="ck_tool_attempt_event_fencing_token"),
        CheckConstraint(
            "event IN ('lease', 'start', 'heartbeat', 'complete', 'fail', 'cancel', "
            "'lease_expire', 'reject')",
            name="ck_tool_attempt_event_type",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_tool_attempt_event_outcome",
        ),
        Index("ix_tool_attempt_events_created", "attempt_id", "created_at"),
    )


_INVOCATION_TRANSITION_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_invocation_transitions_no_update
    BEFORE UPDATE ON tool_invocation_transitions
    BEGIN
        SELECT RAISE(ABORT, 'tool invocation transitions are append-only');
    END
    """
).execute_if(dialect="sqlite")
_INVOCATION_TRANSITION_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_invocation_transitions_no_delete
    BEFORE DELETE ON tool_invocation_transitions
    BEGIN
        SELECT RAISE(ABORT, 'tool invocation transitions are append-only');
    END
    """
).execute_if(dialect="sqlite")
_INVOCATION_TRANSITION_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_tool_invocation_transition_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'tool invocation transitions are append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_INVOCATION_TRANSITION_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_invocation_transitions_no_mutation
    BEFORE UPDATE OR DELETE ON tool_invocation_transitions
    FOR EACH ROW EXECUTE FUNCTION reject_tool_invocation_transition_mutation()
    """
).execute_if(dialect="postgresql")
_INVOCATION_TRANSITION_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_tool_invocation_transition_mutation()"
).execute_if(dialect="postgresql")

event.listen(
    ToolInvocationTransition.__table__,
    "after_create",
    _INVOCATION_TRANSITION_SQLITE_UPDATE_TRIGGER,
)
event.listen(
    ToolInvocationTransition.__table__,
    "after_create",
    _INVOCATION_TRANSITION_SQLITE_DELETE_TRIGGER,
)
event.listen(
    ToolInvocationTransition.__table__,
    "after_create",
    _INVOCATION_TRANSITION_POSTGRES_FUNCTION,
)
event.listen(
    ToolInvocationTransition.__table__,
    "after_create",
    _INVOCATION_TRANSITION_POSTGRES_TRIGGER,
)
event.listen(
    ToolInvocationTransition.__table__,
    "after_drop",
    _INVOCATION_TRANSITION_POSTGRES_FUNCTION_DROP,
)


_ATTEMPT_EVENT_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_attempt_events_no_update
    BEFORE UPDATE ON tool_attempt_events
    BEGIN
        SELECT RAISE(ABORT, 'tool attempt events are append-only');
    END
    """
).execute_if(dialect="sqlite")
_ATTEMPT_EVENT_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_attempt_events_no_delete
    BEFORE DELETE ON tool_attempt_events
    BEGIN
        SELECT RAISE(ABORT, 'tool attempt events are append-only');
    END
    """
).execute_if(dialect="sqlite")
_ATTEMPT_EVENT_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_tool_attempt_event_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'tool attempt events are append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_ATTEMPT_EVENT_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_attempt_events_no_mutation
    BEFORE UPDATE OR DELETE ON tool_attempt_events
    FOR EACH ROW EXECUTE FUNCTION reject_tool_attempt_event_mutation()
    """
).execute_if(dialect="postgresql")
_ATTEMPT_EVENT_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_tool_attempt_event_mutation()"
).execute_if(dialect="postgresql")

event.listen(ToolAttemptEvent.__table__, "after_create", _ATTEMPT_EVENT_SQLITE_UPDATE_TRIGGER)
event.listen(ToolAttemptEvent.__table__, "after_create", _ATTEMPT_EVENT_SQLITE_DELETE_TRIGGER)
event.listen(ToolAttemptEvent.__table__, "after_create", _ATTEMPT_EVENT_POSTGRES_FUNCTION)
event.listen(ToolAttemptEvent.__table__, "after_create", _ATTEMPT_EVENT_POSTGRES_TRIGGER)
event.listen(ToolAttemptEvent.__table__, "after_drop", _ATTEMPT_EVENT_POSTGRES_FUNCTION_DROP)
