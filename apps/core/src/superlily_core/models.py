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


class RenderDocumentRecord(Base):
    """Immutable render request identity with a bounded terminal state."""

    __tablename__ = "render_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(320), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(512))
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    document_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    render_duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("instance_id", "idempotency_key", name="uq_render_document_idempotency"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_render_document_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND safe_error_code IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND safe_error_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND safe_error_code IS NOT NULL)",
            name="ck_render_document_terminal",
        ),
        CheckConstraint(
            "render_duration_ms IS NULL OR render_duration_ms BETWEEN 0 AND 120000",
            name="ck_render_document_duration",
        ),
        Index("ix_render_documents_conversation_created", "conversation_key", "created_at"),
    )


class RenderAttemptRecord(Base):
    """Fenced render execution; stale running attempts may be abandoned and retried."""

    __tablename__ = "render_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    render_id: Mapped[str] = mapped_column(
        ForeignKey("render_documents.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    renderer_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    renderer_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    renderer_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    render_duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("render_id", "attempt_number", name="uq_render_attempt_number"),
        UniqueConstraint("render_id", "fencing_token", name="uq_render_attempt_fence"),
        CheckConstraint(
            "state IN ('running', 'succeeded', 'failed', 'abandoned')",
            name="ck_render_attempt_state",
        ),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL AND safe_error_code IS NULL) OR "
            "(state = 'succeeded' AND completed_at IS NOT NULL AND safe_error_code IS NULL) OR "
            "(state IN ('failed', 'abandoned') AND completed_at IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name="ck_render_attempt_terminal",
        ),
        CheckConstraint(
            "render_duration_ms IS NULL OR render_duration_ms BETWEEN 0 AND 120000",
            name="ck_render_attempt_duration",
        ),
        Index("ix_render_attempts_render_started", "render_id", "started_at"),
    )


class RenderArtifactRecord(Base):
    __tablename__ = "render_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    render_id: Mapped[str] = mapped_column(
        ForeignKey("render_documents.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("render_attempts.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(256), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width_pixels: Mapped[int] = mapped_column(Integer, nullable=False)
    height_pixels: Mapped[int] = mapped_column(Integer, nullable=False)
    producer_kind: Mapped[str] = mapped_column(
        String(32), default="document_renderer", nullable=False
    )
    producer_id: Mapped[str] = mapped_column(
        String(128), default="xelatex-document-v1", nullable=False
    )
    source_invocation_id: Mapped[str | None] = mapped_column(String(36))
    data_classification: Mapped[str] = mapped_column(
        String(32), default="conversation", nullable=False
    )
    canonical_scope: Mapped[str] = mapped_column(String(512), nullable=False)
    safe_filename: Mapped[str] = mapped_column(
        String(255), default="rendered-document.png", nullable=False
    )
    accessibility_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_reason: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("mime_type = 'image/png'", name="ck_render_artifact_mime"),
        CheckConstraint("byte_size BETWEEN 1 AND 8388608", name="ck_render_artifact_bytes"),
        CheckConstraint(
            "width_pixels BETWEEN 1 AND 4096 AND height_pixels BETWEEN 1 AND 4096",
            name="ck_render_artifact_dimensions",
        ),
        CheckConstraint("expires_at > created_at", name="ck_render_artifact_expiry"),
        CheckConstraint(
            "retention_until >= expires_at", name="ck_render_artifact_retention"
        ),
        CheckConstraint(
            "data_classification IN ('public', 'conversation', 'sensitive', 'administrative')",
            name="ck_render_artifact_classification",
        ),
        CheckConstraint(
            "(content_deleted_at IS NULL AND deletion_reason IS NULL) OR "
            "(content_deleted_at IS NOT NULL AND deletion_reason IS NOT NULL)",
            name="ck_render_artifact_deletion",
        ),
        Index("ix_render_artifacts_hash", "content_sha256"),
        Index("ix_render_artifacts_render_created", "render_id", "created_at"),
        Index(
            "ix_render_artifacts_retention",
            "content_deleted_at",
            "retention_until",
        ),
    )


class RenderDeliveryPlan(Base):
    """Immutable capability decision created before an adapter send."""

    __tablename__ = "render_delivery_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("render_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(320), nullable=False)
    capability_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_family: Mapped[str] = mapped_column(String(32), nullable=False)
    fallback_text: Mapped[str | None] = mapped_column(Text)
    degradation_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    decision_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_document_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    selected_alternatives_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    rejected_alternatives_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    ordered_payloads_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "capability_hash", name="uq_render_delivery_plan_capability"
        ),
        CheckConstraint(
            "selected_family IN ('image', 'text')",
            name="ck_render_delivery_plan_family",
        ),
        CheckConstraint("expires_at > created_at", name="ck_render_delivery_plan_expiry"),
        Index("ix_render_delivery_plan_instance_created", "instance_id", "created_at"),
    )


class RenderDeliveryIntent(Base):
    """Idempotent pre-send claim; pending expiry becomes ambiguous, never a blind retry."""

    __tablename__ = "render_delivery_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("render_delivery_plans.id", ondelete="RESTRICT"), nullable=False
    )
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(String(320), nullable=False)
    capability_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ordered_payloads_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False
    )
    reply_to_platform_message_id: Mapped[str | None] = mapped_column(String(512))
    mention_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(512))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("instance_id", "idempotency_key", name="uq_render_delivery_intent_key"),
        CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'ambiguous')",
            name="ck_render_delivery_intent_status",
        ),
        CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND platform_message_id IS NULL "
            "AND safe_error_code IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL "
            "AND platform_message_id IS NOT NULL AND safe_error_code IS NULL) OR "
            "(status IN ('failed', 'ambiguous') AND completed_at IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name="ck_render_delivery_intent_terminal",
        ),
        Index("ix_render_delivery_intent_deadline", "status", "deadline_at"),
    )


class RenderDeliveryAttempt(Base):
    """Append-only evidence reported by the adapter after a platform send."""

    __tablename__ = "render_delivery_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("render_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    instance_id: Mapped[str] = mapped_column(
        ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False
    )
    plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("render_delivery_plans.id", ondelete="RESTRICT")
    )
    intent_id: Mapped[str | None] = mapped_column(
        ForeignKey("render_delivery_intents.id", ondelete="RESTRICT"), unique=True
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_message_id: Mapped[str | None] = mapped_column(String(512))
    safe_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('succeeded', 'failed', 'ambiguous')",
            name="ck_render_delivery_outcome",
        ),
        CheckConstraint(
            "(outcome = 'succeeded' AND platform_message_id IS NOT NULL AND safe_error_code IS NULL) OR "
            "(outcome <> 'succeeded' AND safe_error_code IS NOT NULL)",
            name="ck_render_delivery_evidence",
        ),
        Index("ix_render_delivery_artifact_created", "artifact_id", "created_at"),
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
    resource_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
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
    resource_version: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
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


class ToolRolloutPlanRecord(Base):
    __tablename__ = "tool_rollout_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    rollback_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    review_status: Mapped[str] = mapped_column(String(32), default="reviewed", nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(32), default="reviewed", nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    max_invocations: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_json: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    plan_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    import_outcome: Mapped[str] = mapped_column(String(32), default="accepted", nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_tool_rollout_plan_identity"),
        UniqueConstraint("plan_hash", name="uq_tool_rollout_plan_hash"),
        CheckConstraint("mode IN ('canary')", name="ck_tool_rollout_plan_mode"),
        CheckConstraint(
            "rollback_mode IN ('ledger_only')",
            name="ck_tool_rollout_plan_rollback_mode",
        ),
        CheckConstraint("review_status IN ('reviewed')", name="ck_tool_rollout_plan_review"),
        CheckConstraint(
            "lifecycle IN ('reviewed', 'active', 'paused')",
            name="ck_tool_rollout_plan_lifecycle",
        ),
        CheckConstraint("resource_version >= 1", name="ck_tool_rollout_plan_version"),
        CheckConstraint(
            "max_invocations >= 1 AND max_invocations <= 1000",
            name="ck_tool_rollout_plan_max_invocations",
        ),
        CheckConstraint("expires_at > starts_at", name="ck_tool_rollout_plan_window"),
        CheckConstraint("import_outcome IN ('accepted')", name="ck_tool_rollout_plan_import"),
        Index("ix_tool_rollout_plans_lifecycle", "lifecycle"),
        Index("ix_tool_rollout_plans_window", "starts_at", "expires_at"),
        Index(
            "uq_tool_rollout_single_active",
            "lifecycle",
            unique=True,
            sqlite_where=sql_text("lifecycle = 'active'"),
            postgresql_where=sql_text("lifecycle = 'active'"),
        ),
    )


class ToolRolloutPlanItemRecord(Base):
    __tablename__ = "tool_rollout_plan_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_record_id: Mapped[str] = mapped_column(
        ForeignKey("tool_rollout_plans.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    descriptor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_conversation: Mapped[str] = mapped_column(String(512), nullable=False)
    caller: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expected_descriptor_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_provider_resource_version: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_record_id", "item_id", name="uq_tool_rollout_plan_item_id"),
        UniqueConstraint(
            "plan_record_id",
            "tool_id",
            "descriptor_version",
            "descriptor_hash",
            "canonical_conversation",
            "caller",
            name="uq_tool_rollout_plan_execution_target",
        ),
        CheckConstraint(
            "caller IN ('command', 'agent', 'admin_api')",
            name="ck_rollout_item_caller",
        ),
        CheckConstraint(
            "expected_descriptor_resource_version >= 1",
            name="ck_rollout_item_descriptor_version",
        ),
        CheckConstraint(
            "expected_provider_resource_version >= 1",
            name="ck_rollout_item_provider_version",
        ),
        Index("ix_tool_rollout_items_tool", "tool_id", "descriptor_version"),
        Index("ix_tool_rollout_items_provider", "provider_id"),
    )


class ToolRolloutPlanLifecycleEvent(Base):
    __tablename__ = "tool_rollout_plan_lifecycle_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_record_id: Mapped[str] = mapped_column(
        ForeignKey("tool_rollout_plans.id", ondelete="CASCADE"), nullable=False
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
        UniqueConstraint(
            "plan_record_id", "sequence", name="uq_tool_rollout_plan_lifecycle_sequence"
        ),
        CheckConstraint(
            "lifecycle IN ('reviewed', 'active', 'paused')",
            name="ck_tool_rollout_plan_event_lifecycle",
        ),
        Index("ix_tool_rollout_plan_events_created", "plan_record_id", "created_at"),
    )


class ToolRolloutPlanCounter(Base):
    __tablename__ = "tool_rollout_plan_counters"

    plan_record_id: Mapped[str] = mapped_column(
        ForeignKey("tool_rollout_plans.id", ondelete="CASCADE"), primary_key=True
    )
    consumed_invocations: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "consumed_invocations >= 0",
            name="ck_tool_rollout_plan_counter_nonnegative",
        ),
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
    rollout_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_rollout_plans.id", ondelete="RESTRICT")
    )
    rollout_plan_item_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_rollout_plan_items.id", ondelete="RESTRICT")
    )
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
            "creator_type IN ('command', 'agent', 'admin_api')",
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
        Index("ix_tool_invocations_rollout_plan", "rollout_plan_id", "created_at"),
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
            "actor_type IN ('command', 'agent', 'admin_api', 'provider', 'reaper', 'system')",
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


class ToolConfirmation(Base):
    __tablename__ = "tool_confirmations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    caller_type: Mapped[str] = mapped_column(String(32), nullable=False)
    caller_id: Mapped[str] = mapped_column(String(128), nullable=False)
    required_approvals: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("invocation_id", name="uq_tool_confirmation_invocation"),
        CheckConstraint(
            "policy IN ('on_write', 'always', 'two_person')",
            name="ck_tool_confirmation_policy",
        ),
        CheckConstraint(
            "caller_type IN ('command', 'admin_api')",
            name="ck_tool_confirmation_caller",
        ),
        CheckConstraint(
            "state IN ('pending', 'consumed', 'rejected', 'expired')",
            name="ck_tool_confirmation_state",
        ),
        CheckConstraint(
            "resource_version >= 1", name="ck_tool_confirmation_resource_version"
        ),
        CheckConstraint(
            "required_approvals >= 1 AND required_approvals <= 2",
            name="ck_tool_confirmation_required_approvals",
        ),
        CheckConstraint("expires_at > created_at", name="ck_tool_confirmation_expiry"),
        CheckConstraint(
            "((state = 'pending' AND consumed_at IS NULL AND rejected_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL AND rejected_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'rejected' AND consumed_at IS NULL AND rejected_at IS NOT NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'expired' AND consumed_at IS NULL AND rejected_at IS NULL "
            "AND expired_at IS NOT NULL))",
            name="ck_tool_confirmation_terminal_time",
        ),
        Index("ix_tool_confirmations_state_expiry", "state", "expires_at"),
    )


class ToolConfirmationEvent(Base):
    __tablename__ = "tool_confirmation_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    confirmation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_confirmations.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "confirmation_id", "sequence", name="uq_tool_confirmation_event_sequence"
        ),
        UniqueConstraint(
            "confirmation_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_tool_confirmation_event_idempotency",
        ),
        CheckConstraint("sequence >= 1", name="ck_tool_confirmation_event_sequence"),
        CheckConstraint(
            "event IN ('create', 'approve', 'reject', 'expire')",
            name="ck_tool_confirmation_event_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'consumed', 'rejected', 'expired')",
            name="ck_tool_confirmation_event_state",
        ),
        CheckConstraint(
            "actor_type IN ('command', 'admin_api', 'reaper', 'system')",
            name="ck_tool_confirmation_event_actor",
        ),
        CheckConstraint(
            "((event = 'create' AND sequence = 1 AND previous_state IS NULL "
            "AND state = 'pending') OR "
            "(event <> 'create' AND sequence > 1 AND previous_state = 'pending'))",
            name="ck_tool_confirmation_event_initial",
        ),
        Index(
            "ix_tool_confirmation_events_created", "confirmation_id", "created_at"
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


class ToolArtifact(Base):
    __tablename__ = "tool_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("tool_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("tool_providers.id", ondelete="RESTRICT"), nullable=False
    )
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    reservation_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    producer_descriptor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    data_classification: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_conversation: Mapped[str] = mapped_column(String(512), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    max_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_width_pixels: Mapped[int] = mapped_column(Integer, nullable=False)
    max_height_pixels: Mapped[int] = mapped_column(Integer, nullable=False)
    declared_bytes: Mapped[int | None] = mapped_column(BigInteger)
    declared_sha256: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    upload_secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    quarantine_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    byte_size: Mapped[int | None] = mapped_column(BigInteger)
    width_pixels: Mapped[int | None] = mapped_column(Integer)
    height_pixels: Mapped[int | None] = mapped_column(Integer)
    storage_key: Mapped[str | None] = mapped_column(String(512))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referenced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_id", "idempotency_key", name="uq_tool_artifact_idempotency"
        ),
        CheckConstraint("fencing_token >= 1", name="ck_tool_artifact_fencing_token"),
        CheckConstraint(
            "data_classification IN ('public', 'conversation', 'sensitive', 'administrative')",
            name="ck_tool_artifact_classification",
        ),
        CheckConstraint(
            "state IN ('reserved', 'uploading', 'finalized', 'rejected', 'expired')",
            name="ck_tool_artifact_state",
        ),
        CheckConstraint(
            "resource_version >= 1", name="ck_tool_artifact_resource_version"
        ),
        CheckConstraint(
            "max_bytes >= 1 AND max_width_pixels >= 1 AND max_height_pixels >= 1",
            name="ck_tool_artifact_bounds",
        ),
        CheckConstraint(
            "declared_bytes IS NULL OR (declared_bytes >= 1 AND declared_bytes <= max_bytes)",
            name="ck_tool_artifact_declared_bytes",
        ),
        CheckConstraint("expires_at > created_at", name="ck_tool_artifact_expiry"),
        CheckConstraint(
            "((width_pixels IS NULL AND height_pixels IS NULL) OR "
            "(width_pixels IS NOT NULL AND height_pixels IS NOT NULL "
            "AND width_pixels >= 1 AND width_pixels <= max_width_pixels "
            "AND height_pixels >= 1 AND height_pixels <= max_height_pixels))",
            name="ck_tool_artifact_dimensions",
        ),
        CheckConstraint(
            "((content_sha256 IS NULL AND byte_size IS NULL) OR "
            "(content_sha256 IS NOT NULL AND byte_size IS NOT NULL "
            "AND byte_size >= 1 AND byte_size <= max_bytes))",
            name="ck_tool_artifact_content",
        ),
        CheckConstraint(
            "((state = 'finalized' AND content_sha256 IS NOT NULL "
            "AND byte_size IS NOT NULL AND storage_key IS NOT NULL "
            "AND finalized_at IS NOT NULL AND rejected_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'rejected' AND rejected_at IS NOT NULL AND finalized_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'expired' AND expired_at IS NOT NULL AND finalized_at IS NULL "
            "AND rejected_at IS NULL) OR "
            "(state IN ('reserved', 'uploading') AND finalized_at IS NULL "
            "AND rejected_at IS NULL AND expired_at IS NULL))",
            name="ck_tool_artifact_terminal_time",
        ),
        Index("ix_tool_artifacts_attempt_state", "attempt_id", "state"),
        Index("ix_tool_artifacts_state_expiry", "state", "expires_at"),
        Index("ix_tool_artifacts_content_hash", "content_sha256"),
    )


class ToolArtifactEvent(Base):
    __tablename__ = "tool_artifact_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("tool_artifacts.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "artifact_id", "sequence", name="uq_tool_artifact_event_sequence"
        ),
        CheckConstraint("sequence >= 1", name="ck_tool_artifact_event_sequence"),
        CheckConstraint("fencing_token >= 1", name="ck_tool_artifact_event_fencing_token"),
        CheckConstraint(
            "event IN ('reserve', 'upload_start', 'upload_complete', 'finalize', "
            "'reference', 'reject', 'expire', 'cleanup')",
            name="ck_tool_artifact_event_type",
        ),
        CheckConstraint(
            "state IN ('reserved', 'uploading', 'finalized', 'rejected', 'expired')",
            name="ck_tool_artifact_event_state",
        ),
        CheckConstraint(
            "actor_type IN ('provider', 'reaper', 'system')",
            name="ck_tool_artifact_event_actor",
        ),
        CheckConstraint(
            "((event = 'reserve' AND sequence = 1 AND previous_state IS NULL "
            "AND state = 'reserved') OR "
            "(event <> 'reserve' AND sequence > 1 AND previous_state IS NOT NULL))",
            name="ck_tool_artifact_event_initial",
        ),
        Index("ix_tool_artifact_events_created", "artifact_id", "created_at"),
    )


_CONTROL_ROLES_SQL = "'auditor', 'operator', 'reviewer', 'security_admin', 'break_glass'"


class ControlPlaneSession(Base):
    __tablename__ = "control_plane_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reauthenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"role IN ({_CONTROL_ROLES_SQL})", name="ck_control_session_role"),
        CheckConstraint("resource_version >= 1", name="ck_control_session_version"),
        CheckConstraint("expires_at > issued_at", name="ck_control_session_expiry"),
        Index("ix_control_sessions_operator_expiry", "operator_id", "expires_at"),
    )


class ControlPlaneLoginAttempt(Base):
    __tablename__ = "control_plane_login_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_lookup_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    client_fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_login_outcome",
        ),
        Index("ix_control_login_operator_time", "operator_lookup_hash", "created_at"),
        Index("ix_control_login_client_time", "client_fingerprint_hash", "created_at"),
    )


class ControlPlaneMutation(Base):
    __tablename__ = "control_plane_mutations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("control_plane_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(512), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    before_hash: Mapped[str | None] = mapped_column(String(64))
    after_hash: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "operator_id",
            "operation",
            "idempotency_key",
            name="uq_control_mutation_idempotency",
        ),
        CheckConstraint(f"role IN ({_CONTROL_ROLES_SQL})", name="ck_control_mutation_role"),
        CheckConstraint("expected_version >= 1", name="ck_control_mutation_version"),
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_mutation_outcome",
        ),
        Index("ix_control_mutations_target_time", "target_type", "target_id", "created_at"),
    )


class ControlPlaneAuditEvent(Base):
    __tablename__ = "control_plane_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("control_plane_sessions.id", ondelete="RESTRICT")
    )
    operator_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            f"role IS NULL OR role IN ({_CONTROL_ROLES_SQL})",
            name="ck_control_audit_role",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_audit_outcome",
        ),
        Index("ix_control_audit_created", "created_at"),
        Index("ix_control_audit_operator_created", "operator_id", "created_at"),
    )


class ControlPlanePreview(Base):
    __tablename__ = "control_plane_previews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("control_plane_sessions.id", ondelete="RESTRICT"), nullable=False
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(512), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sql_text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"role IN ({_CONTROL_ROLES_SQL})", name="ck_control_preview_role"),
        CheckConstraint("expected_version >= 1", name="ck_control_preview_version"),
        Index("ix_control_previews_target_created", "target_type", "target_id", "created_at"),
        Index("ix_control_previews_session_expiry", "session_id", "expires_at"),
    )


class AgentModelProfileRecord(Base):
    """Immutable reviewed model-provider data-handling authority."""

    __tablename__ = "agent_model_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_commit: Mapped[str] = mapped_column(String(40), nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "provider_id",
            "version",
            name="uq_agent_model_profile_version",
        ),
        UniqueConstraint("profile_hash", name="uq_agent_model_profile_hash"),
        Index("ix_agent_model_profiles_provider", "provider_id", "version"),
    )


class AgentRun(Base):
    """One bounded planner-only response attempt with zero execution authority."""

    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    creator_type: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str] = mapped_column(
        ForeignKey("source_events.id", ondelete="RESTRICT"),
        nullable=False,
    )
    conversation_key: Mapped[str] = mapped_column(String(512), nullable=False)
    principal_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    principal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    context_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context_recipe_version: Mapped[str] = mapped_column(String(128), nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    eligible_tools_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    eligible_tools_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    budget_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_profile_id: Mapped[str] = mapped_column(
        ForeignKey("agent_model_profiles.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_profile_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fallback_model_profiles_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
    )
    fallback_model_profiles_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    routing_reason: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_invocation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_intent_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "creator_type",
            "creator_id",
            "idempotency_key",
            name="uq_agent_run_idempotency",
        ),
        CheckConstraint(
            "creator_type IN ('admin_api', 'system')",
            name="ck_agent_run_creator_type",
        ),
        CheckConstraint("mode = 'shadow'", name="ck_agent_run_mode"),
        CheckConstraint(
            "state IN ('context_ready', 'model_running', 'shadow_complete', "
            "'rejected', 'failed', 'timed_out', 'budget_exhausted', 'cancelled')",
            name="ck_agent_run_state",
        ),
        CheckConstraint("resource_version >= 1", name="ck_agent_run_resource_version"),
        CheckConstraint("attempt_count >= 0", name="ck_agent_run_attempt_count"),
        CheckConstraint(
            "tool_invocation_count = 0",
            name="ck_agent_run_zero_tool_execution",
        ),
        CheckConstraint(
            "delivery_intent_count = 0",
            name="ck_agent_run_zero_delivery",
        ),
        CheckConstraint(
            "((state IN ('shadow_complete', 'rejected', 'failed', 'timed_out', "
            "'budget_exhausted', 'cancelled') AND terminal_at IS NOT NULL) OR "
            "(state IN ('context_ready', 'model_running') AND terminal_at IS NULL))",
            name="ck_agent_run_terminal",
        ),
        Index("ix_agent_runs_source_created", "source_event_id", "created_at"),
        Index("ix_agent_runs_state_deadline", "state", "deadline_at"),
        Index("ix_agent_runs_model_profile", "model_profile_id", "created_at"),
    )


class AgentRunEvent(Base):
    __tablename__ = "agent_run_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
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
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_agent_run_event_sequence"),
        CheckConstraint("sequence >= 1", name="ck_agent_run_event_sequence"),
        CheckConstraint(
            "event IN ('context_ready', 'model_start', 'model_retry', "
            "'shadow_complete', 'reject', 'fail', 'timeout', "
            "'budget_exhaust', 'cancel')",
            name="ck_agent_run_event_type",
        ),
        CheckConstraint(
            "state IN ('context_ready', 'model_running', 'shadow_complete', "
            "'rejected', 'failed', 'timed_out', 'budget_exhausted', 'cancelled')",
            name="ck_agent_run_event_state",
        ),
        CheckConstraint(
            "actor_type IN ('admin_api', 'model_provider', 'system')",
            name="ck_agent_run_event_actor",
        ),
        Index("ix_agent_run_events_created", "run_id", "created_at"),
    )


class AgentRunAttempt(Base):
    """Terminal model-attempt evidence; retries insert another row."""

    __tablename__ = "agent_run_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    model_profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    model_request_id: Mapped[str | None] = mapped_column(String(256))
    raw_output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True)
    )
    proposal_hash: Mapped[str | None] = mapped_column(String(64))
    usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    usage_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_error_code: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "attempt_number",
            name="uq_agent_run_attempt_number",
        ),
        UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_agent_run_attempt_idempotency",
        ),
        CheckConstraint("attempt_number >= 1", name="ck_agent_run_attempt_number"),
        CheckConstraint(
            "outcome IN ('succeeded', 'provider_error', 'invalid_output', "
            "'timed_out', 'cancelled')",
            name="ck_agent_run_attempt_outcome",
        ),
        CheckConstraint(
            "completed_at >= started_at",
            name="ck_agent_run_attempt_time",
        ),
        CheckConstraint(
            "((outcome = 'succeeded' AND proposal_json IS NOT NULL "
            "AND proposal_hash IS NOT NULL AND safe_error_code IS NULL) OR "
            "(outcome <> 'succeeded' AND proposal_json IS NULL "
            "AND proposal_hash IS NULL AND safe_error_code IS NOT NULL))",
            name="ck_agent_run_attempt_result",
        ),
        Index("ix_agent_run_attempts_run_created", "run_id", "created_at"),
    )


class AgentToolProposalRecord(Base):
    __tablename__ = "agent_tool_proposals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("agent_run_attempts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    descriptor_version: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "attempt_id",
            "ordinal",
            name="uq_agent_tool_proposal_ordinal",
        ),
        CheckConstraint("ordinal >= 0", name="ck_agent_tool_proposal_ordinal"),
        CheckConstraint(
            "validation IN ('valid', 'invalid_arguments', "
            "'forbidden_tool', 'duplicate_loop')",
            name="ck_agent_tool_proposal_validation",
        ),
        Index("ix_agent_tool_proposals_run_created", "run_id", "created_at"),
        Index("ix_agent_tool_proposals_tool", "tool_id", "descriptor_version"),
    )


class AgentToolLoop(Base):
    """One explicitly promoted Phase 5b proposal and its bounded continuation."""

    __tablename__ = "agent_tool_loops"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="RESTRICT"), nullable=False
    )
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tool_proposals.id", ondelete="RESTRICT"), nullable=False
    )
    invocation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_invocations.id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_version: Mapped[int] = mapped_column(Integer, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    result_json: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    result_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_agent_tool_loop_run"),
        UniqueConstraint("proposal_id", name="uq_agent_tool_loop_proposal"),
        UniqueConstraint("invocation_id", name="uq_agent_tool_loop_invocation"),
        CheckConstraint(
            "state IN ('tool_pending', 'result_ready', 'complete', 'failed', "
            "'budget_exhausted')",
            name="ck_agent_tool_loop_state",
        ),
        CheckConstraint("resource_version >= 1", name="ck_agent_tool_loop_version"),
        CheckConstraint("result_bytes >= 0", name="ck_agent_tool_loop_result_bytes"),
        CheckConstraint(
            "((state = 'result_ready' AND result_json IS NOT NULL "
            "AND result_hash IS NOT NULL AND terminal_at IS NULL) OR "
            "(state IN ('complete', 'failed', 'budget_exhausted') "
            "AND terminal_at IS NOT NULL) OR "
            "(state = 'tool_pending' AND result_json IS NULL "
            "AND result_hash IS NULL AND terminal_at IS NULL))",
            name="ck_agent_tool_loop_result_state",
        ),
        Index("ix_agent_tool_loops_state", "state", "updated_at"),
    )


class AgentToolLoopEvent(Base):
    __tablename__ = "agent_tool_loop_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    loop_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tool_loops.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(128), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("loop_id", "sequence", name="uq_agent_tool_loop_event"),
        CheckConstraint("sequence >= 1", name="ck_agent_tool_loop_event_sequence"),
    )


class AgentToolContinuation(Base):
    __tablename__ = "agent_tool_continuations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    loop_id: Mapped[str] = mapped_column(
        ForeignKey("agent_tool_loops.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=sql_text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "loop_id",
            "attempt_number",
            name="uq_agent_tool_continuation_attempt",
        ),
        UniqueConstraint(
            "provider_id",
            "idempotency_key",
            name="uq_agent_tool_continuation_idempotency",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_agent_tool_continuation_attempt",
        ),
    )


_CONFIRMATION_SQLITE_AUTHORITY_UPDATE = DDL(
    """
    CREATE TRIGGER tool_confirmations_authority_no_update
    BEFORE UPDATE OF invocation_id, policy, request_hash, input_hash, principal_hash,
        policy_hash, caller_type, caller_id, required_approvals, expires_at, created_at
    ON tool_confirmations
    BEGIN
        SELECT RAISE(ABORT, 'tool confirmation authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_CONFIRMATION_SQLITE_DELETE = DDL(
    """
    CREATE TRIGGER tool_confirmations_no_delete
    BEFORE DELETE ON tool_confirmations
    BEGIN
        SELECT RAISE(ABORT, 'tool confirmation evidence cannot be deleted');
    END
    """
).execute_if(dialect="sqlite")
_CONFIRMATION_SQLITE_STATE_GUARD = DDL(
    """
    CREATE TRIGGER tool_confirmations_state_guard
    BEFORE UPDATE OF state, resource_version, consumed_at, rejected_at, expired_at, updated_at
    ON tool_confirmations
    WHEN NEW.state IS NOT OLD.state
      OR NEW.resource_version IS NOT OLD.resource_version
      OR NEW.consumed_at IS NOT OLD.consumed_at
      OR NEW.rejected_at IS NOT OLD.rejected_at
      OR NEW.expired_at IS NOT OLD.expired_at
      OR NEW.updated_at IS NOT OLD.updated_at
    BEGIN
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'tool confirmation resource version must increase by one') END;
        SELECT CASE WHEN NOT (
            (OLD.state = 'pending' AND NEW.state = 'consumed') OR
            (OLD.state = 'pending' AND NEW.state = 'rejected') OR
            (OLD.state = 'pending' AND NEW.state = 'expired')
        ) THEN RAISE(ABORT, 'tool confirmation state transition is not allowed') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM tool_confirmation_events
            WHERE confirmation_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_state = OLD.state
              AND state = NEW.state
              AND effective_at IS NEW.updated_at
              AND ((event = 'approve' AND NEW.state = 'consumed'
                    AND NEW.consumed_at IS effective_at) OR
                   (event = 'reject' AND NEW.state = 'rejected'
                    AND NEW.rejected_at IS effective_at) OR
                   (event = 'expire' AND NEW.state = 'expired'
                    AND NEW.expired_at IS effective_at))
        ) THEN RAISE(ABORT, 'tool confirmation event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_CONFIRMATION_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_confirmation_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'tool confirmation evidence cannot be deleted';
        END IF;
        IF NEW.invocation_id IS DISTINCT FROM OLD.invocation_id
           OR NEW.policy IS DISTINCT FROM OLD.policy
           OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
           OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
           OR NEW.principal_hash IS DISTINCT FROM OLD.principal_hash
           OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
           OR NEW.caller_type IS DISTINCT FROM OLD.caller_type
           OR NEW.caller_id IS DISTINCT FROM OLD.caller_id
           OR NEW.required_approvals IS DISTINCT FROM OLD.required_approvals
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'tool confirmation authority is immutable';
        END IF;
        IF NEW.state IS DISTINCT FROM OLD.state
           OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
           OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
           OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
           OR NEW.expired_at IS DISTINCT FROM OLD.expired_at
           OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
            IF NEW.resource_version != OLD.resource_version + 1 THEN
                RAISE EXCEPTION 'tool confirmation resource version must increase by one';
            END IF;
            IF NOT (
                (OLD.state = 'pending' AND NEW.state = 'consumed') OR
                (OLD.state = 'pending' AND NEW.state = 'rejected') OR
                (OLD.state = 'pending' AND NEW.state = 'expired')
            ) THEN
                RAISE EXCEPTION 'tool confirmation state transition is not allowed';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_confirmation_events
                WHERE confirmation_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_state = OLD.state
                  AND state = NEW.state
                  AND effective_at IS NOT DISTINCT FROM NEW.updated_at
                  AND ((event = 'approve' AND NEW.state = 'consumed'
                        AND NEW.consumed_at IS NOT DISTINCT FROM effective_at) OR
                       (event = 'reject' AND NEW.state = 'rejected'
                        AND NEW.rejected_at IS NOT DISTINCT FROM effective_at) OR
                       (event = 'expire' AND NEW.state = 'expired'
                        AND NEW.expired_at IS NOT DISTINCT FROM effective_at))
            ) THEN
                RAISE EXCEPTION 'tool confirmation event is required';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_CONFIRMATION_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_confirmations_guard
    BEFORE UPDATE OR DELETE ON tool_confirmations
    FOR EACH ROW EXECUTE FUNCTION guard_tool_confirmation_mutation()
    """
).execute_if(dialect="postgresql")
_CONFIRMATION_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_confirmation_mutation()"
).execute_if(dialect="postgresql")

for _confirmation_guard in (
    _CONFIRMATION_SQLITE_AUTHORITY_UPDATE,
    _CONFIRMATION_SQLITE_DELETE,
    _CONFIRMATION_SQLITE_STATE_GUARD,
    _CONFIRMATION_POSTGRES_FUNCTION,
    _CONFIRMATION_POSTGRES_TRIGGER,
):
    event.listen(ToolConfirmation.__table__, "after_create", _confirmation_guard)
event.listen(ToolConfirmation.__table__, "after_drop", _CONFIRMATION_POSTGRES_DROP)


_ARTIFACT_SQLITE_AUTHORITY_UPDATE = DDL(
    """
    CREATE TRIGGER tool_artifacts_authority_no_update
    BEFORE UPDATE OF invocation_id, attempt_id, provider_id, fencing_token,
        idempotency_key, reservation_request_hash, producer_tool_id,
        producer_descriptor_version, producer_descriptor_hash, data_classification,
        canonical_conversation, mime_type, policy_snapshot_json, policy_hash,
        max_bytes, max_width_pixels, max_height_pixels, declared_bytes, declared_sha256,
        expires_at, upload_secret_hash, quarantine_key, created_at
    ON tool_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'tool artifact authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_ARTIFACT_SQLITE_DELETE = DDL(
    """
    CREATE TRIGGER tool_artifacts_no_delete
    BEFORE DELETE ON tool_artifacts
    BEGIN
        SELECT RAISE(ABORT, 'tool artifact evidence cannot be deleted');
    END
    """
).execute_if(dialect="sqlite")
_ARTIFACT_SQLITE_STATE_GUARD = DDL(
    """
    CREATE TRIGGER tool_artifacts_state_guard
    BEFORE UPDATE OF state, resource_version, content_sha256, byte_size, width_pixels,
        height_pixels, storage_key, finalized_at, referenced_at, rejected_at,
        expired_at, content_deleted_at, updated_at
    ON tool_artifacts
    WHEN NEW.state IS NOT OLD.state
      OR NEW.resource_version IS NOT OLD.resource_version
      OR NEW.content_sha256 IS NOT OLD.content_sha256
      OR NEW.byte_size IS NOT OLD.byte_size
      OR NEW.width_pixels IS NOT OLD.width_pixels
      OR NEW.height_pixels IS NOT OLD.height_pixels
      OR NEW.storage_key IS NOT OLD.storage_key
      OR NEW.finalized_at IS NOT OLD.finalized_at
      OR NEW.referenced_at IS NOT OLD.referenced_at
      OR NEW.rejected_at IS NOT OLD.rejected_at
      OR NEW.expired_at IS NOT OLD.expired_at
      OR NEW.content_deleted_at IS NOT OLD.content_deleted_at
      OR NEW.updated_at IS NOT OLD.updated_at
    BEGIN
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'tool artifact resource version must increase by one') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM tool_artifact_events
            WHERE artifact_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_state = OLD.state
              AND state = NEW.state
              AND provider_id = OLD.provider_id
              AND fencing_token = OLD.fencing_token
              AND effective_at IS NEW.updated_at
              AND ((event = 'upload_start' AND OLD.state = 'reserved'
                    AND NEW.state = 'uploading'
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'upload_complete' AND OLD.state = 'uploading'
                    AND NEW.state = 'uploading'
                    AND OLD.content_sha256 IS NULL AND OLD.byte_size IS NULL
                    AND OLD.width_pixels IS NULL AND OLD.height_pixels IS NULL
                    AND NEW.content_sha256 IS NOT NULL AND NEW.byte_size IS NOT NULL
                    AND NEW.width_pixels IS NOT NULL AND NEW.height_pixels IS NOT NULL
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'finalize' AND OLD.state = 'uploading'
                    AND NEW.state = 'finalized' AND OLD.finalized_at IS NULL
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS NOT NULL
                    AND NEW.finalized_at IS effective_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'reference' AND OLD.state = 'finalized'
                    AND NEW.state = 'finalized' AND OLD.referenced_at IS NULL
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS effective_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'reject' AND OLD.state IN ('reserved', 'uploading')
                    AND NEW.state = 'rejected' AND OLD.rejected_at IS NULL
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS effective_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'expire' AND OLD.state IN ('reserved', 'uploading')
                    AND NEW.state = 'expired' AND OLD.expired_at IS NULL
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS effective_at
                    AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                   (event = 'cleanup' AND NEW.state = OLD.state
                    AND OLD.content_deleted_at IS NULL
                    AND NEW.content_sha256 IS OLD.content_sha256
                    AND NEW.byte_size IS OLD.byte_size
                    AND NEW.width_pixels IS OLD.width_pixels
                    AND NEW.height_pixels IS OLD.height_pixels
                    AND NEW.storage_key IS OLD.storage_key
                    AND NEW.finalized_at IS OLD.finalized_at
                    AND NEW.referenced_at IS OLD.referenced_at
                    AND NEW.rejected_at IS OLD.rejected_at
                    AND NEW.expired_at IS OLD.expired_at
                    AND NEW.content_deleted_at IS effective_at))
        ) THEN RAISE(ABORT, 'tool artifact event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_ARTIFACT_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_artifact_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'tool artifact evidence cannot be deleted';
        END IF;
        IF NEW.invocation_id IS DISTINCT FROM OLD.invocation_id
           OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
           OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
           OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.reservation_request_hash IS DISTINCT FROM OLD.reservation_request_hash
           OR NEW.producer_tool_id IS DISTINCT FROM OLD.producer_tool_id
           OR NEW.producer_descriptor_version IS DISTINCT FROM OLD.producer_descriptor_version
           OR NEW.producer_descriptor_hash IS DISTINCT FROM OLD.producer_descriptor_hash
           OR NEW.data_classification IS DISTINCT FROM OLD.data_classification
           OR NEW.canonical_conversation IS DISTINCT FROM OLD.canonical_conversation
           OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
           OR NEW.policy_snapshot_json::text IS DISTINCT FROM OLD.policy_snapshot_json::text
           OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
           OR NEW.max_bytes IS DISTINCT FROM OLD.max_bytes
           OR NEW.max_width_pixels IS DISTINCT FROM OLD.max_width_pixels
           OR NEW.max_height_pixels IS DISTINCT FROM OLD.max_height_pixels
           OR NEW.declared_bytes IS DISTINCT FROM OLD.declared_bytes
           OR NEW.declared_sha256 IS DISTINCT FROM OLD.declared_sha256
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.upload_secret_hash IS DISTINCT FROM OLD.upload_secret_hash
           OR NEW.quarantine_key IS DISTINCT FROM OLD.quarantine_key
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'tool artifact authority is immutable';
        END IF;
        IF NEW.state IS DISTINCT FROM OLD.state
           OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
           OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
           OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
           OR NEW.width_pixels IS DISTINCT FROM OLD.width_pixels
           OR NEW.height_pixels IS DISTINCT FROM OLD.height_pixels
           OR NEW.storage_key IS DISTINCT FROM OLD.storage_key
           OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
           OR NEW.referenced_at IS DISTINCT FROM OLD.referenced_at
           OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
           OR NEW.expired_at IS DISTINCT FROM OLD.expired_at
           OR NEW.content_deleted_at IS DISTINCT FROM OLD.content_deleted_at
           OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
            IF NEW.resource_version != OLD.resource_version + 1 THEN
                RAISE EXCEPTION 'tool artifact resource version must increase by one';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_artifact_events
                WHERE artifact_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_state = OLD.state
                  AND state = NEW.state
                  AND provider_id = OLD.provider_id
                  AND fencing_token = OLD.fencing_token
                  AND effective_at IS NOT DISTINCT FROM NEW.updated_at
                  AND ((event = 'upload_start' AND OLD.state = 'reserved'
                        AND NEW.state = 'uploading'
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'upload_complete' AND OLD.state = 'uploading'
                        AND NEW.state = 'uploading'
                        AND OLD.content_sha256 IS NULL AND OLD.byte_size IS NULL
                        AND OLD.width_pixels IS NULL AND OLD.height_pixels IS NULL
                        AND NEW.content_sha256 IS NOT NULL AND NEW.byte_size IS NOT NULL
                        AND NEW.width_pixels IS NOT NULL AND NEW.height_pixels IS NOT NULL
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'finalize' AND OLD.state = 'uploading'
                        AND NEW.state = 'finalized' AND OLD.finalized_at IS NULL
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT NULL
                        AND NEW.finalized_at IS NOT DISTINCT FROM effective_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'reference' AND OLD.state = 'finalized'
                        AND NEW.state = 'finalized' AND OLD.referenced_at IS NULL
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM effective_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'reject' AND OLD.state IN ('reserved', 'uploading')
                        AND NEW.state = 'rejected' AND OLD.rejected_at IS NULL
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM effective_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'expire' AND OLD.state IN ('reserved', 'uploading')
                        AND NEW.state = 'expired' AND OLD.expired_at IS NULL
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM effective_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                       (event = 'cleanup' AND NEW.state = OLD.state
                        AND OLD.content_deleted_at IS NULL
                        AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                        AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                        AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                        AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                        AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                        AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                        AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                        AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                        AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                        AND NEW.content_deleted_at IS NOT DISTINCT FROM effective_at))
            ) THEN
                RAISE EXCEPTION 'tool artifact event is required';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_ARTIFACT_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_artifacts_guard
    BEFORE UPDATE OR DELETE ON tool_artifacts
    FOR EACH ROW EXECUTE FUNCTION guard_tool_artifact_mutation()
    """
).execute_if(dialect="postgresql")
_ARTIFACT_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_artifact_mutation()"
).execute_if(dialect="postgresql")

for _artifact_guard in (
    _ARTIFACT_SQLITE_AUTHORITY_UPDATE,
    _ARTIFACT_SQLITE_DELETE,
    _ARTIFACT_SQLITE_STATE_GUARD,
    _ARTIFACT_POSTGRES_FUNCTION,
    _ARTIFACT_POSTGRES_TRIGGER,
):
    event.listen(ToolArtifact.__table__, "after_create", _artifact_guard)
event.listen(ToolArtifact.__table__, "after_drop", _ARTIFACT_POSTGRES_DROP)


_CONFIRMATION_ARTIFACT_EVENT_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_confirmation_artifact_event_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'confirmation and artifact events are append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_CONFIRMATION_ARTIFACT_EVENT_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_confirmation_artifact_event_mutation()"
).execute_if(dialect="postgresql")


def _install_confirmation_artifact_event_triggers(table) -> None:
    name = table.name
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_update
            BEFORE UPDATE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'confirmation and artifact events are append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_delete
            BEFORE DELETE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'confirmation and artifact events are append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_mutation
            BEFORE UPDATE OR DELETE ON {name}
            FOR EACH ROW EXECUTE FUNCTION reject_confirmation_artifact_event_mutation()
            """
        ).execute_if(dialect="postgresql"),
    )


event.listen(
    ToolConfirmationEvent.__table__,
    "after_create",
    _CONFIRMATION_ARTIFACT_EVENT_POSTGRES_FUNCTION,
)
for _confirmation_artifact_event_table in (
    ToolConfirmationEvent.__table__,
    ToolArtifactEvent.__table__,
):
    _install_confirmation_artifact_event_triggers(_confirmation_artifact_event_table)
event.listen(
    ToolConfirmationEvent.__table__,
    "after_drop",
    _CONFIRMATION_ARTIFACT_EVENT_POSTGRES_DROP,
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


_DESCRIPTOR_AUTHORITY_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptors_authority_no_update
    BEFORE UPDATE OF tool_id, version, descriptor_hash, schema_profile, source_plugin,
        review_status, source_commit, bundle_hash, reviewer, canonical_json,
        descriptor_json, import_outcome, imported_at
    ON tool_descriptors
    BEGIN
        SELECT RAISE(ABORT, 'descriptor authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_DESCRIPTOR_AUTHORITY_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptors_no_delete
    BEFORE DELETE ON tool_descriptors
    BEGIN
        SELECT RAISE(ABORT, 'descriptor authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_DESCRIPTOR_LIFECYCLE_SQLITE_GUARD = DDL(
    """
    CREATE TRIGGER tool_descriptors_lifecycle_guard
    BEFORE UPDATE OF lifecycle, resource_version ON tool_descriptors
    WHEN NEW.lifecycle != OLD.lifecycle OR NEW.resource_version != OLD.resource_version
    BEGIN
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'descriptor resource version must increase by one') END;
        SELECT CASE WHEN NOT (
            (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
            (OLD.lifecycle = 'active' AND NEW.lifecycle = 'suspended') OR
            (OLD.lifecycle = 'suspended' AND NEW.lifecycle = 'active')
        ) THEN RAISE(ABORT, 'descriptor lifecycle transition is not allowed') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM tool_descriptor_lifecycle_events
            WHERE descriptor_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_lifecycle = OLD.lifecycle
              AND lifecycle = NEW.lifecycle
        ) THEN RAISE(ABORT, 'descriptor lifecycle event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_DESCRIPTOR_AUTHORITY_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_descriptor_authority_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'descriptor authority is immutable';
        END IF;
        IF NEW.tool_id IS DISTINCT FROM OLD.tool_id
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.descriptor_hash IS DISTINCT FROM OLD.descriptor_hash
           OR NEW.schema_profile IS DISTINCT FROM OLD.schema_profile
           OR NEW.source_plugin IS DISTINCT FROM OLD.source_plugin
           OR NEW.review_status IS DISTINCT FROM OLD.review_status
           OR NEW.source_commit IS DISTINCT FROM OLD.source_commit
           OR NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
           OR NEW.reviewer IS DISTINCT FROM OLD.reviewer
           OR NEW.canonical_json IS DISTINCT FROM OLD.canonical_json
           OR NEW.descriptor_json::text IS DISTINCT FROM OLD.descriptor_json::text
           OR NEW.import_outcome IS DISTINCT FROM OLD.import_outcome
           OR NEW.imported_at IS DISTINCT FROM OLD.imported_at THEN
            RAISE EXCEPTION 'descriptor authority is immutable';
        END IF;
        IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
           OR NEW.resource_version IS DISTINCT FROM OLD.resource_version THEN
            IF NEW.resource_version != OLD.resource_version + 1 THEN
                RAISE EXCEPTION 'descriptor resource version must increase by one';
            END IF;
            IF NOT (
                (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                (OLD.lifecycle = 'active' AND NEW.lifecycle = 'suspended') OR
                (OLD.lifecycle = 'suspended' AND NEW.lifecycle = 'active')
            ) THEN
                RAISE EXCEPTION 'descriptor lifecycle transition is not allowed';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_descriptor_lifecycle_events
                WHERE descriptor_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_lifecycle = OLD.lifecycle
                  AND lifecycle = NEW.lifecycle
            ) THEN
                RAISE EXCEPTION 'descriptor lifecycle event is required';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_DESCRIPTOR_AUTHORITY_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptors_authority_guard
    BEFORE UPDATE OR DELETE ON tool_descriptors
    FOR EACH ROW EXECUTE FUNCTION guard_tool_descriptor_authority_mutation()
    """
).execute_if(dialect="postgresql")
_DESCRIPTOR_AUTHORITY_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_descriptor_authority_mutation()"
).execute_if(dialect="postgresql")

for _descriptor_guard in (
    _DESCRIPTOR_AUTHORITY_SQLITE_UPDATE_TRIGGER,
    _DESCRIPTOR_AUTHORITY_SQLITE_DELETE_TRIGGER,
    _DESCRIPTOR_LIFECYCLE_SQLITE_GUARD,
    _DESCRIPTOR_AUTHORITY_POSTGRES_FUNCTION,
    _DESCRIPTOR_AUTHORITY_POSTGRES_TRIGGER,
):
    event.listen(ToolDescriptorRecord.__table__, "after_create", _descriptor_guard)
event.listen(
    ToolDescriptorRecord.__table__,
    "after_drop",
    _DESCRIPTOR_AUTHORITY_POSTGRES_FUNCTION_DROP,
)


_DESCRIPTOR_EVENT_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptor_lifecycle_events_no_update
    BEFORE UPDATE ON tool_descriptor_lifecycle_events
    BEGIN
        SELECT RAISE(ABORT, 'descriptor lifecycle evidence is append-only');
    END
    """
).execute_if(dialect="sqlite")
_DESCRIPTOR_EVENT_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptor_lifecycle_events_no_delete
    BEFORE DELETE ON tool_descriptor_lifecycle_events
    BEGIN
        SELECT RAISE(ABORT, 'descriptor lifecycle evidence is append-only');
    END
    """
).execute_if(dialect="sqlite")
_DESCRIPTOR_EVENT_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_descriptor_lifecycle_evidence_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'descriptor lifecycle evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_DESCRIPTOR_EVENT_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_descriptor_lifecycle_events_no_mutation
    BEFORE UPDATE OR DELETE ON tool_descriptor_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION reject_descriptor_lifecycle_evidence_mutation()
    """
).execute_if(dialect="postgresql")
_DESCRIPTOR_EVENT_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_descriptor_lifecycle_evidence_mutation()"
).execute_if(dialect="postgresql")

for _descriptor_event_guard in (
    _DESCRIPTOR_EVENT_SQLITE_UPDATE_TRIGGER,
    _DESCRIPTOR_EVENT_SQLITE_DELETE_TRIGGER,
    _DESCRIPTOR_EVENT_POSTGRES_FUNCTION,
    _DESCRIPTOR_EVENT_POSTGRES_TRIGGER,
):
    event.listen(ToolDescriptorLifecycleEvent.__table__, "after_create", _descriptor_event_guard)
event.listen(
    ToolDescriptorLifecycleEvent.__table__,
    "after_drop",
    _DESCRIPTOR_EVENT_POSTGRES_FUNCTION_DROP,
)


_PROVIDER_AUTHORITY_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_providers_authority_no_update
    BEFORE UPDATE OF id, owner, allowed_protocols_json, tool_selectors_json, created_at
    ON tool_providers
    BEGIN
        SELECT RAISE(ABORT, 'provider authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_PROVIDER_AUTHORITY_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_providers_no_delete
    BEFORE DELETE ON tool_providers
    BEGIN
        SELECT RAISE(ABORT, 'provider authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_PROVIDER_LIFECYCLE_SQLITE_GUARD = DDL(
    """
    CREATE TRIGGER tool_providers_lifecycle_guard
    BEFORE UPDATE OF lifecycle, resource_version ON tool_providers
    WHEN NEW.lifecycle != OLD.lifecycle OR NEW.resource_version != OLD.resource_version
    BEGIN
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'provider resource version must increase by one') END;
        SELECT CASE WHEN NOT (
            (OLD.lifecycle = 'active' AND NEW.lifecycle = 'quarantined') OR
            (OLD.lifecycle = 'quarantined' AND NEW.lifecycle = 'active')
        ) THEN RAISE(ABORT, 'provider lifecycle transition is not allowed') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM tool_provider_lifecycle_events
            WHERE provider_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_lifecycle = OLD.lifecycle
              AND lifecycle = NEW.lifecycle
        ) THEN RAISE(ABORT, 'provider lifecycle event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_PROVIDER_AUTHORITY_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_provider_authority_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'provider authority is immutable';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.owner IS DISTINCT FROM OLD.owner
           OR NEW.allowed_protocols_json::text IS DISTINCT FROM OLD.allowed_protocols_json::text
           OR NEW.tool_selectors_json::text IS DISTINCT FROM OLD.tool_selectors_json::text
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'provider authority is immutable';
        END IF;
        IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
           OR NEW.resource_version IS DISTINCT FROM OLD.resource_version THEN
            IF NEW.resource_version != OLD.resource_version + 1 THEN
                RAISE EXCEPTION 'provider resource version must increase by one';
            END IF;
            IF NOT (
                (OLD.lifecycle = 'active' AND NEW.lifecycle = 'quarantined') OR
                (OLD.lifecycle = 'quarantined' AND NEW.lifecycle = 'active')
            ) THEN
                RAISE EXCEPTION 'provider lifecycle transition is not allowed';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_provider_lifecycle_events
                WHERE provider_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_lifecycle = OLD.lifecycle
                  AND lifecycle = NEW.lifecycle
            ) THEN
                RAISE EXCEPTION 'provider lifecycle event is required';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_PROVIDER_AUTHORITY_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_providers_authority_guard
    BEFORE UPDATE OR DELETE ON tool_providers
    FOR EACH ROW EXECUTE FUNCTION guard_tool_provider_authority_mutation()
    """
).execute_if(dialect="postgresql")
_PROVIDER_AUTHORITY_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_provider_authority_mutation()"
).execute_if(dialect="postgresql")

for _provider_guard in (
    _PROVIDER_AUTHORITY_SQLITE_UPDATE_TRIGGER,
    _PROVIDER_AUTHORITY_SQLITE_DELETE_TRIGGER,
    _PROVIDER_LIFECYCLE_SQLITE_GUARD,
    _PROVIDER_AUTHORITY_POSTGRES_FUNCTION,
    _PROVIDER_AUTHORITY_POSTGRES_TRIGGER,
):
    event.listen(ToolProvider.__table__, "after_create", _provider_guard)
event.listen(
    ToolProvider.__table__,
    "after_drop",
    _PROVIDER_AUTHORITY_POSTGRES_FUNCTION_DROP,
)


_PROVIDER_EVENT_SQLITE_UPDATE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_provider_lifecycle_events_no_update
    BEFORE UPDATE ON tool_provider_lifecycle_events
    BEGIN
        SELECT RAISE(ABORT, 'provider lifecycle evidence is append-only');
    END
    """
).execute_if(dialect="sqlite")
_PROVIDER_EVENT_SQLITE_DELETE_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_provider_lifecycle_events_no_delete
    BEFORE DELETE ON tool_provider_lifecycle_events
    BEGIN
        SELECT RAISE(ABORT, 'provider lifecycle evidence is append-only');
    END
    """
).execute_if(dialect="sqlite")
_PROVIDER_EVENT_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_provider_lifecycle_evidence_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'provider lifecycle evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_PROVIDER_EVENT_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_provider_lifecycle_events_no_mutation
    BEFORE UPDATE OR DELETE ON tool_provider_lifecycle_events
    FOR EACH ROW EXECUTE FUNCTION reject_provider_lifecycle_evidence_mutation()
    """
).execute_if(dialect="postgresql")
_PROVIDER_EVENT_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_provider_lifecycle_evidence_mutation()"
).execute_if(dialect="postgresql")

for _provider_event_guard in (
    _PROVIDER_EVENT_SQLITE_UPDATE_TRIGGER,
    _PROVIDER_EVENT_SQLITE_DELETE_TRIGGER,
    _PROVIDER_EVENT_POSTGRES_FUNCTION,
    _PROVIDER_EVENT_POSTGRES_TRIGGER,
):
    event.listen(ToolProviderLifecycleEvent.__table__, "after_create", _provider_event_guard)
event.listen(
    ToolProviderLifecycleEvent.__table__,
    "after_drop",
    _PROVIDER_EVENT_POSTGRES_FUNCTION_DROP,
)


_ROLLOUT_PLAN_SQLITE_AUTHORITY_UPDATE = DDL(
    """
    CREATE TRIGGER tool_rollout_plans_authority_no_update
    BEFORE UPDATE OF plan_id, version, plan_hash, schema_version, mode, rollback_mode,
        review_status, starts_at, expires_at, max_invocations, reason, source_commit,
        bundle_hash, reviewer, canonical_json, plan_json, import_outcome, imported_at
    ON tool_rollout_plans
    BEGIN
        SELECT RAISE(ABORT, 'rollout plan authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_ROLLOUT_PLAN_SQLITE_DELETE = DDL(
    """
    CREATE TRIGGER tool_rollout_plans_no_delete
    BEFORE DELETE ON tool_rollout_plans
    BEGIN
        SELECT RAISE(ABORT, 'rollout plan authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_ROLLOUT_PLAN_SQLITE_LIFECYCLE_GUARD = DDL(
    """
    CREATE TRIGGER tool_rollout_plans_lifecycle_guard
    BEFORE UPDATE OF lifecycle, resource_version, updated_at ON tool_rollout_plans
    WHEN NEW.lifecycle != OLD.lifecycle
      OR NEW.resource_version != OLD.resource_version
      OR NEW.updated_at != OLD.updated_at
    BEGIN
        SELECT CASE WHEN NEW.lifecycle = OLD.lifecycle
            THEN RAISE(ABORT, 'rollout plan lifecycle change is required') END;
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'rollout plan resource version must increase by one') END;
        SELECT CASE WHEN NOT (
            (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
            (OLD.lifecycle = 'active' AND NEW.lifecycle = 'paused') OR
            (OLD.lifecycle = 'paused' AND NEW.lifecycle = 'active')
        ) THEN RAISE(ABORT, 'rollout plan lifecycle transition is not allowed') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM tool_rollout_plan_lifecycle_events
            WHERE plan_record_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_lifecycle = OLD.lifecycle
              AND lifecycle = NEW.lifecycle
        ) THEN RAISE(ABORT, 'rollout plan lifecycle event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_ROLLOUT_PLAN_POSTGRES_AUTHORITY_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_rollout_plan_authority_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'rollout plan authority is immutable';
        END IF;
        IF NEW.plan_id IS DISTINCT FROM OLD.plan_id
           OR NEW.version IS DISTINCT FROM OLD.version
           OR NEW.plan_hash IS DISTINCT FROM OLD.plan_hash
           OR NEW.schema_version IS DISTINCT FROM OLD.schema_version
           OR NEW.mode IS DISTINCT FROM OLD.mode
           OR NEW.rollback_mode IS DISTINCT FROM OLD.rollback_mode
           OR NEW.review_status IS DISTINCT FROM OLD.review_status
           OR NEW.starts_at IS DISTINCT FROM OLD.starts_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
           OR NEW.max_invocations IS DISTINCT FROM OLD.max_invocations
           OR NEW.reason IS DISTINCT FROM OLD.reason
           OR NEW.source_commit IS DISTINCT FROM OLD.source_commit
           OR NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
           OR NEW.reviewer IS DISTINCT FROM OLD.reviewer
           OR NEW.canonical_json IS DISTINCT FROM OLD.canonical_json
           OR NEW.plan_json::text IS DISTINCT FROM OLD.plan_json::text
           OR NEW.import_outcome IS DISTINCT FROM OLD.import_outcome
           OR NEW.imported_at IS DISTINCT FROM OLD.imported_at THEN
            RAISE EXCEPTION 'rollout plan authority is immutable';
        END IF;
        IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
           OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
           OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
            IF NEW.lifecycle IS NOT DISTINCT FROM OLD.lifecycle THEN
                RAISE EXCEPTION 'rollout plan lifecycle change is required';
            END IF;
            IF NEW.resource_version != OLD.resource_version + 1 THEN
                RAISE EXCEPTION 'rollout plan resource version must increase by one';
            END IF;
            IF NOT (
                (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                (OLD.lifecycle = 'active' AND NEW.lifecycle = 'paused') OR
                (OLD.lifecycle = 'paused' AND NEW.lifecycle = 'active')
            ) THEN
                RAISE EXCEPTION 'rollout plan lifecycle transition is not allowed';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM tool_rollout_plan_lifecycle_events
                WHERE plan_record_id = OLD.id
                  AND sequence = NEW.resource_version
                  AND previous_lifecycle = OLD.lifecycle
                  AND lifecycle = NEW.lifecycle
            ) THEN
                RAISE EXCEPTION 'rollout plan lifecycle event is required';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_ROLLOUT_PLAN_POSTGRES_AUTHORITY_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_rollout_plans_authority_guard
    BEFORE UPDATE OR DELETE ON tool_rollout_plans
    FOR EACH ROW EXECUTE FUNCTION guard_tool_rollout_plan_authority_mutation()
    """
).execute_if(dialect="postgresql")
_ROLLOUT_PLAN_POSTGRES_AUTHORITY_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_rollout_plan_authority_mutation()"
).execute_if(dialect="postgresql")

for _rollout_plan_guard in (
    _ROLLOUT_PLAN_SQLITE_AUTHORITY_UPDATE,
    _ROLLOUT_PLAN_SQLITE_DELETE,
    _ROLLOUT_PLAN_SQLITE_LIFECYCLE_GUARD,
    _ROLLOUT_PLAN_POSTGRES_AUTHORITY_FUNCTION,
    _ROLLOUT_PLAN_POSTGRES_AUTHORITY_TRIGGER,
):
    event.listen(ToolRolloutPlanRecord.__table__, "after_create", _rollout_plan_guard)
event.listen(
    ToolRolloutPlanRecord.__table__,
    "after_drop",
    _ROLLOUT_PLAN_POSTGRES_AUTHORITY_DROP,
)


_ROLLOUT_EVIDENCE_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_rollout_plan_evidence_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'rollout plan evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_ROLLOUT_EVIDENCE_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_rollout_plan_evidence_mutation()"
).execute_if(dialect="postgresql")


def _install_rollout_append_only_triggers(table) -> None:
    name = table.name
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_update
            BEFORE UPDATE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'rollout plan evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_delete
            BEFORE DELETE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'rollout plan evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_mutation
            BEFORE UPDATE OR DELETE ON {name}
            FOR EACH ROW EXECUTE FUNCTION reject_rollout_plan_evidence_mutation()
            """
        ).execute_if(dialect="postgresql"),
    )


event.listen(
    ToolRolloutPlanItemRecord.__table__,
    "after_create",
    _ROLLOUT_EVIDENCE_POSTGRES_FUNCTION,
)
for _rollout_evidence_table in (
    ToolRolloutPlanItemRecord.__table__,
    ToolRolloutPlanLifecycleEvent.__table__,
):
    _install_rollout_append_only_triggers(_rollout_evidence_table)
event.listen(
    ToolRolloutPlanRecord.__table__,
    "after_drop",
    _ROLLOUT_EVIDENCE_POSTGRES_DROP,
)


_ROLLOUT_COUNTER_SQLITE_UPDATE = DDL(
    """
    CREATE TRIGGER tool_rollout_plan_counters_update_guard
    BEFORE UPDATE ON tool_rollout_plan_counters
    BEGIN
        SELECT CASE WHEN NEW.plan_record_id != OLD.plan_record_id
            THEN RAISE(ABORT, 'rollout plan counter identity is immutable') END;
        SELECT CASE WHEN NEW.consumed_invocations != OLD.consumed_invocations + 1
            THEN RAISE(ABORT, 'rollout plan counter must increase by one') END;
        SELECT CASE WHEN NEW.last_consumed_at IS NULL
            THEN RAISE(ABORT, 'rollout plan consumption time is required') END;
    END
    """
).execute_if(dialect="sqlite")
_ROLLOUT_COUNTER_SQLITE_DELETE = DDL(
    """
    CREATE TRIGGER tool_rollout_plan_counters_no_delete
    BEFORE DELETE ON tool_rollout_plan_counters
    BEGIN
        SELECT RAISE(ABORT, 'rollout plan counter cannot be deleted');
    END
    """
).execute_if(dialect="sqlite")
_ROLLOUT_COUNTER_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_tool_rollout_plan_counter_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'rollout plan counter cannot be deleted';
        END IF;
        IF NEW.plan_record_id IS DISTINCT FROM OLD.plan_record_id THEN
            RAISE EXCEPTION 'rollout plan counter identity is immutable';
        END IF;
        IF NEW.consumed_invocations != OLD.consumed_invocations + 1 THEN
            RAISE EXCEPTION 'rollout plan counter must increase by one';
        END IF;
        IF NEW.last_consumed_at IS NULL THEN
            RAISE EXCEPTION 'rollout plan consumption time is required';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_ROLLOUT_COUNTER_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER tool_rollout_plan_counters_guard
    BEFORE UPDATE OR DELETE ON tool_rollout_plan_counters
    FOR EACH ROW EXECUTE FUNCTION guard_tool_rollout_plan_counter_mutation()
    """
).execute_if(dialect="postgresql")
_ROLLOUT_COUNTER_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_tool_rollout_plan_counter_mutation()"
).execute_if(dialect="postgresql")

for _rollout_counter_guard in (
    _ROLLOUT_COUNTER_SQLITE_UPDATE,
    _ROLLOUT_COUNTER_SQLITE_DELETE,
    _ROLLOUT_COUNTER_POSTGRES_FUNCTION,
    _ROLLOUT_COUNTER_POSTGRES_TRIGGER,
):
    event.listen(ToolRolloutPlanCounter.__table__, "after_create", _rollout_counter_guard)
event.listen(
    ToolRolloutPlanCounter.__table__,
    "after_drop",
    _ROLLOUT_COUNTER_POSTGRES_DROP,
)


_CONTROL_EVIDENCE_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_control_plane_evidence_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'control plane evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_CONTROL_EVIDENCE_POSTGRES_FUNCTION_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_control_plane_evidence_mutation()"
).execute_if(dialect="postgresql")


def _install_control_evidence_triggers(table) -> None:
    name = table.name
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_update
            BEFORE UPDATE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'control plane evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_delete
            BEFORE DELETE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'control plane evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_mutation
            BEFORE UPDATE OR DELETE ON {name}
            FOR EACH ROW EXECUTE FUNCTION reject_control_plane_evidence_mutation()
            """
        ).execute_if(dialect="postgresql"),
    )


event.listen(
    ControlPlaneLoginAttempt.__table__,
    "after_create",
    _CONTROL_EVIDENCE_POSTGRES_FUNCTION,
)
for _control_evidence_table in (
    ControlPlaneLoginAttempt.__table__,
    ControlPlaneMutation.__table__,
    ControlPlaneAuditEvent.__table__,
    ControlPlanePreview.__table__,
):
    _install_control_evidence_triggers(_control_evidence_table)
event.listen(
    ControlPlaneLoginAttempt.__table__,
    "after_drop",
    _CONTROL_EVIDENCE_POSTGRES_FUNCTION_DROP,
)


_RENDER_DELIVERY_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_render_delivery_attempt_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'render delivery evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_RENDER_DELIVERY_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_render_delivery_attempt_mutation()"
).execute_if(dialect="postgresql")
event.listen(
    RenderDeliveryAttempt.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER render_delivery_attempts_no_update
        BEFORE UPDATE ON render_delivery_attempts
        BEGIN SELECT RAISE(ABORT, 'render delivery evidence is append-only'); END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(
    RenderDeliveryAttempt.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER render_delivery_attempts_no_delete
        BEFORE DELETE ON render_delivery_attempts
        BEGIN SELECT RAISE(ABORT, 'render delivery evidence is append-only'); END
        """
    ).execute_if(dialect="sqlite"),
)
event.listen(RenderDeliveryAttempt.__table__, "after_create", _RENDER_DELIVERY_POSTGRES_FUNCTION)
event.listen(
    RenderDeliveryAttempt.__table__,
    "after_create",
    DDL(
        """
        CREATE TRIGGER render_delivery_attempts_no_mutation
        BEFORE UPDATE OR DELETE ON render_delivery_attempts
        FOR EACH ROW EXECUTE FUNCTION reject_render_delivery_attempt_mutation()
        """
    ).execute_if(dialect="postgresql"),
)
event.listen(RenderDeliveryAttempt.__table__, "after_drop", _RENDER_DELIVERY_POSTGRES_DROP)


_AGENT_EVIDENCE_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION reject_agent_evidence_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'agent evidence is append-only';
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_AGENT_EVIDENCE_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS reject_agent_evidence_mutation()"
).execute_if(dialect="postgresql")


def _install_agent_append_only_triggers(table) -> None:
    name = table.name
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_update
            BEFORE UPDATE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'agent evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_delete
            BEFORE DELETE ON {name}
            BEGIN
                SELECT RAISE(ABORT, 'agent evidence is append-only');
            END
            """
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        table,
        "after_create",
        DDL(
            f"""
            CREATE TRIGGER {name}_no_mutation
            BEFORE UPDATE OR DELETE ON {name}
            FOR EACH ROW EXECUTE FUNCTION reject_agent_evidence_mutation()
            """
        ).execute_if(dialect="postgresql"),
    )


event.listen(
    AgentModelProfileRecord.__table__,
    "after_create",
    _AGENT_EVIDENCE_POSTGRES_FUNCTION,
)
for _agent_evidence_table in (
    AgentModelProfileRecord.__table__,
    AgentRunEvent.__table__,
    AgentRunAttempt.__table__,
    AgentToolProposalRecord.__table__,
):
    _install_agent_append_only_triggers(_agent_evidence_table)
event.listen(
    AgentModelProfileRecord.__table__,
    "after_drop",
    _AGENT_EVIDENCE_POSTGRES_DROP,
)


_AGENT_RUN_SQLITE_AUTHORITY_UPDATE = DDL(
    """
    CREATE TRIGGER agent_runs_authority_no_update
    BEFORE UPDATE OF creator_type, creator_id, idempotency_key, request_hash,
        source_event_id, conversation_key, principal_snapshot_json, principal_hash,
        context_snapshot_json, context_recipe_version, context_hash,
        eligible_tools_json, eligible_tools_hash,
        budget_snapshot_json, budget_hash, model_profile_id,
        model_profile_snapshot_json, model_profile_hash,
        fallback_model_profiles_json, fallback_model_profiles_hash,
        routing_reason, mode,
        tool_invocation_count, delivery_intent_count, deadline_at, created_at
    ON agent_runs
    BEGIN
        SELECT RAISE(ABORT, 'agent run authority is immutable');
    END
    """
).execute_if(dialect="sqlite")
_AGENT_RUN_SQLITE_DELETE = DDL(
    """
    CREATE TRIGGER agent_runs_no_delete
    BEFORE DELETE ON agent_runs
    BEGIN
        SELECT RAISE(ABORT, 'agent run evidence cannot be deleted');
    END
    """
).execute_if(dialect="sqlite")
_AGENT_RUN_SQLITE_STATE_GUARD = DDL(
    """
    CREATE TRIGGER agent_runs_state_guard
    BEFORE UPDATE OF state, resource_version, attempt_count, reason_code,
        terminal_at, updated_at
    ON agent_runs
    BEGIN
        SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
            THEN RAISE(ABORT, 'agent run resource version must increase by one') END;
        SELECT CASE WHEN NOT (
            (OLD.state = 'context_ready' AND NEW.state = 'model_running') OR
            (OLD.state = 'model_running' AND NEW.state = 'context_ready') OR
            (OLD.state = 'model_running' AND NEW.state = 'shadow_complete') OR
            (OLD.state = 'model_running' AND NEW.state = 'failed') OR
            (OLD.state = 'model_running' AND NEW.state = 'timed_out') OR
            (OLD.state = 'model_running' AND NEW.state = 'budget_exhausted') OR
            (OLD.state = 'model_running' AND NEW.state = 'cancelled')
        ) THEN RAISE(ABORT, 'agent run state transition is not allowed') END;
        SELECT CASE WHEN (
            OLD.state = 'context_ready'
            AND NEW.state = 'model_running'
            AND NEW.attempt_count != OLD.attempt_count + 1
        ) OR (
            OLD.state = 'model_running'
            AND NEW.attempt_count != OLD.attempt_count
        ) THEN RAISE(ABORT, 'agent run attempt count is invalid') END;
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM agent_run_events
            WHERE run_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_state = OLD.state
              AND state = NEW.state
        ) THEN RAISE(ABORT, 'agent run event is required') END;
    END
    """
).execute_if(dialect="sqlite")
_AGENT_RUN_POSTGRES_FUNCTION = DDL(
    """
    CREATE OR REPLACE FUNCTION guard_agent_run_mutation()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'agent run evidence cannot be deleted';
        END IF;
        IF NEW.creator_type IS DISTINCT FROM OLD.creator_type
           OR NEW.creator_id IS DISTINCT FROM OLD.creator_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
           OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
           OR NEW.conversation_key IS DISTINCT FROM OLD.conversation_key
           OR NEW.principal_snapshot_json::text IS DISTINCT FROM OLD.principal_snapshot_json::text
           OR NEW.principal_hash IS DISTINCT FROM OLD.principal_hash
           OR NEW.context_snapshot_json::text IS DISTINCT FROM OLD.context_snapshot_json::text
           OR NEW.context_recipe_version IS DISTINCT FROM OLD.context_recipe_version
           OR NEW.context_hash IS DISTINCT FROM OLD.context_hash
           OR NEW.eligible_tools_json::text IS DISTINCT FROM OLD.eligible_tools_json::text
           OR NEW.eligible_tools_hash IS DISTINCT FROM OLD.eligible_tools_hash
           OR NEW.budget_snapshot_json::text IS DISTINCT FROM OLD.budget_snapshot_json::text
           OR NEW.budget_hash IS DISTINCT FROM OLD.budget_hash
           OR NEW.model_profile_id IS DISTINCT FROM OLD.model_profile_id
           OR NEW.model_profile_snapshot_json::text IS DISTINCT FROM OLD.model_profile_snapshot_json::text
           OR NEW.model_profile_hash IS DISTINCT FROM OLD.model_profile_hash
           OR NEW.fallback_model_profiles_json::text IS DISTINCT FROM OLD.fallback_model_profiles_json::text
           OR NEW.fallback_model_profiles_hash IS DISTINCT FROM OLD.fallback_model_profiles_hash
           OR NEW.routing_reason IS DISTINCT FROM OLD.routing_reason
           OR NEW.mode IS DISTINCT FROM OLD.mode
           OR NEW.tool_invocation_count IS DISTINCT FROM OLD.tool_invocation_count
           OR NEW.delivery_intent_count IS DISTINCT FROM OLD.delivery_intent_count
           OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'agent run authority is immutable';
        END IF;
        IF NEW.resource_version != OLD.resource_version + 1 THEN
            RAISE EXCEPTION 'agent run resource version must increase by one';
        END IF;
        IF NOT (
            (OLD.state = 'context_ready' AND NEW.state = 'model_running') OR
            (OLD.state = 'model_running' AND NEW.state IN (
                'context_ready', 'shadow_complete', 'failed', 'timed_out',
                'budget_exhausted', 'cancelled'
            ))
        ) THEN
            RAISE EXCEPTION 'agent run state transition is not allowed';
        END IF;
        IF (OLD.state = 'context_ready'
            AND NEW.state = 'model_running'
            AND NEW.attempt_count != OLD.attempt_count + 1)
           OR (OLD.state = 'model_running'
               AND NEW.attempt_count != OLD.attempt_count) THEN
            RAISE EXCEPTION 'agent run attempt count is invalid';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM agent_run_events
            WHERE run_id = OLD.id
              AND sequence = NEW.resource_version
              AND previous_state = OLD.state
              AND state = NEW.state
        ) THEN
            RAISE EXCEPTION 'agent run event is required';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """
).execute_if(dialect="postgresql")
_AGENT_RUN_POSTGRES_TRIGGER = DDL(
    """
    CREATE TRIGGER agent_runs_guard
    BEFORE UPDATE OR DELETE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION guard_agent_run_mutation()
    """
).execute_if(dialect="postgresql")
_AGENT_RUN_POSTGRES_DROP = DDL(
    "DROP FUNCTION IF EXISTS guard_agent_run_mutation()"
).execute_if(dialect="postgresql")

for _agent_run_guard in (
    _AGENT_RUN_SQLITE_AUTHORITY_UPDATE,
    _AGENT_RUN_SQLITE_DELETE,
    _AGENT_RUN_SQLITE_STATE_GUARD,
    _AGENT_RUN_POSTGRES_FUNCTION,
    _AGENT_RUN_POSTGRES_TRIGGER,
):
    event.listen(AgentRun.__table__, "after_create", _agent_run_guard)
event.listen(AgentRun.__table__, "after_drop", _AGENT_RUN_POSTGRES_DROP)
