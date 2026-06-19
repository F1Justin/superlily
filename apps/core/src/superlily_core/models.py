from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    __table_args__ = (Index("ix_source_events_occurred_at", "occurred_at"),)


class EventObservation(Base):
    __tablename__ = "event_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_event_id: Mapped[str] = mapped_column(ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False)
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
        Index("ix_event_observations_received_at", "received_at"),
        Index("ix_event_observations_source_event_id", "source_event_id"),
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

