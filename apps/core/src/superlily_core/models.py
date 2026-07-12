from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
