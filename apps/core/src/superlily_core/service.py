import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import exists, or_, select, text as sql_text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn, EventReference, HeartbeatIn, ResponseIn, SanitizationPolicy, sanitize_payload

from .correlation import (
    CORRELATION_VERSION,
    advisory_lock_key,
    canonical_conversation_id,
    canonical_event_type,
    event_correlation_fingerprint,
)
from .models import BotInstance, EventLink, EventObservation, InstanceStatusTransition, ResponseRecord, SourceEvent, utc_now
from .settings import Settings


@dataclass
class _LocalLockEntry:
    lock: asyncio.Lock
    users: int = 0


_LOCAL_LOCKS: dict[str, _LocalLockEntry] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _metadata_policy(settings: Settings) -> SanitizationPolicy:
    return SanitizationPolicy(enabled=True, max_bytes=min(settings.raw_max_bytes, 16_384), max_string=2_048)


def _raw_policy(settings: Settings) -> SanitizationPolicy:
    return SanitizationPolicy(enabled=settings.raw_enabled, max_bytes=settings.raw_max_bytes)


def _dump_list(items: list[Any], settings: Settings) -> list[dict[str, Any]]:
    payload = {"items": [item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in items]}
    sanitized = sanitize_payload(payload, _metadata_policy(settings)) or {"items": []}
    value = sanitized.get("items", [])
    return value if isinstance(value, list) else []


def _reported_status(heartbeat: HeartbeatIn) -> str:
    if heartbeat.process_status in {"stopped", "stopping"}:
        return "offline"
    if heartbeat.process_status == "running" and heartbeat.connection_status == "connected":
        return "online"
    if heartbeat.process_status in {"running", "starting"}:
        return "degraded"
    if heartbeat.process_status == "error":
        return "error"
    return "unknown"


async def ensure_instance(
    session: AsyncSession,
    instance: Any,
    settings: Settings,
) -> BotInstance:
    values = {
        "id": instance.instance_id,
        "platform": instance.platform,
        "adapter": instance.adapter,
        "bot_id": instance.bot_id,
        "role": instance.role,
        "display_name": instance.display_name,
        "version": instance.version,
        "reported_status": "unknown",
        "first_seen_at": utc_now(),
        "metadata_json": {},
    }
    updates = {
        "platform": instance.platform,
        "adapter": instance.adapter,
        "bot_id": instance.bot_id,
        "role": instance.role,
        "display_name": instance.display_name,
        "version": instance.version,
    }
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(BotInstance).values(**values).on_conflict_do_update(
            index_elements=[BotInstance.id], set_=updates
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(BotInstance).values(**values).on_conflict_do_update(
            index_elements=[BotInstance.id], set_=updates
        )
    else:
        record = await session.get(BotInstance, instance.instance_id)
        if record is None:
            record = BotInstance(**values)
            session.add(record)
            await session.flush()
            return record
        for key, value in updates.items():
            setattr(record, key, value)
        await session.flush()
        return record
    await session.execute(statement)
    record = await session.get(BotInstance, instance.instance_id, populate_existing=True)
    assert record is not None
    return record


@asynccontextmanager
async def _correlation_guard(session: AsyncSession, fingerprint: str | None):
    if fingerprint is None:
        yield
        return
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            sql_text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": advisory_lock_key(fingerprint)},
        )
        yield
        return

    with _LOCAL_LOCKS_GUARD:
        entry = _LOCAL_LOCKS.get(fingerprint)
        if entry is None:
            entry = _LocalLockEntry(asyncio.Lock())
            _LOCAL_LOCKS[fingerprint] = entry
        entry.users += 1
    await entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with _LOCAL_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0 and _LOCAL_LOCKS.get(fingerprint) is entry:
                del _LOCAL_LOCKS[fingerprint]


async def _existing_observation(
    session: AsyncSession,
    instance_id: str,
    idempotency_key: str,
    reported_source_event_id: str,
) -> EventObservation | None:
    return await session.scalar(
        select(EventObservation).where(
            EventObservation.instance_id == instance_id,
            or_(
                EventObservation.idempotency_key == idempotency_key,
                EventObservation.reported_source_event_id == reported_source_event_id,
            ),
        )
    )


async def ensure_source_event(
    session: AsyncSession,
    payload: EventIn,
    fingerprint: str | None,
    conversation_id: str,
    correlation_window_seconds: int,
) -> SourceEvent:
    if fingerprint is not None and correlation_window_seconds > 0:
        observed_by_same_instance = exists(
            select(EventObservation.id).where(
                EventObservation.source_event_id == SourceEvent.id,
                EventObservation.instance_id == payload.instance.instance_id,
            )
        )
        window = timedelta(seconds=correlation_window_seconds)
        candidates = (
            await session.scalars(
                select(SourceEvent)
                .where(
                    SourceEvent.correlation_fingerprint == fingerprint,
                    SourceEvent.occurred_at >= payload.occurred_at - window,
                    SourceEvent.occurred_at <= payload.occurred_at + window,
                    ~observed_by_same_instance,
                )
                .order_by(SourceEvent.first_received_at)
                .limit(2)
            )
        ).all()
        if len(candidates) == 1:
            return candidates[0]

    source = SourceEvent(
        id=f"event:{uuid4()}",
        platform=payload.instance.platform,
        event_type=canonical_event_type(payload.instance.platform, payload.event_type),
        conversation_id=conversation_id,
        conversation_type=payload.conversation.type,
        message_id=None,
        correlation_fingerprint=fingerprint,
        correlation_version=CORRELATION_VERSION if fingerprint is not None else None,
        occurred_at=payload.occurred_at,
        first_received_at=utc_now(),
    )
    session.add(source)
    await session.flush()
    return source


def _reference_conversation(payload: EventIn, reference: EventReference) -> tuple[str, str]:
    conversation_type = reference.conversation_type or payload.conversation.type
    conversation_id = canonical_conversation_id(
        payload.instance.platform,
        conversation_type,
        reference.conversation_id or payload.conversation.id,
    )
    return conversation_id, conversation_type


async def _resolve_reference_target(
    session: AsyncSession,
    payload: EventIn,
    observation: EventObservation,
    reference: EventReference,
    target_conversation_id: str,
    target_conversation_type: str,
) -> tuple[str | None, str]:
    if reference.source_event_id:
        source = await session.get(SourceEvent, reference.source_event_id)
        if source is not None:
            return source.id, "resolved"

        candidates = (
            await session.scalars(
                select(EventObservation.source_event_id)
                .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
                .where(
                    EventObservation.instance_id == payload.instance.instance_id,
                    EventObservation.reported_source_event_id == reference.source_event_id,
                    SourceEvent.platform == payload.instance.platform,
                )
                .limit(2)
            )
        ).all()
        if len(candidates) == 1:
            return candidates[0], "resolved"
        if len(candidates) > 1:
            return None, "ambiguous"

    if reference.platform_message_id:
        candidates = (
            await session.scalars(
                select(EventObservation.source_event_id)
                .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
                .where(
                    EventObservation.id != observation.id,
                    EventObservation.instance_id == payload.instance.instance_id,
                    EventObservation.platform_message_id == reference.platform_message_id,
                    SourceEvent.platform == payload.instance.platform,
                    SourceEvent.conversation_id == target_conversation_id,
                    SourceEvent.conversation_type == target_conversation_type,
                    SourceEvent.occurred_at <= payload.occurred_at,
                )
                .order_by(SourceEvent.occurred_at.desc())
                .limit(2)
            )
        ).all()
        if len(candidates) == 1:
            return candidates[0], "resolved"
        if len(candidates) > 1:
            return None, "ambiguous"

    return None, "unresolved"


async def record_event_links(
    session: AsyncSession,
    payload: EventIn,
    observation: EventObservation,
    settings: Settings,
) -> None:
    for reference in payload.references:
        target_conversation_id, target_conversation_type = _reference_conversation(payload, reference)
        to_source_event_id, resolver_status = await _resolve_reference_target(
            session,
            payload,
            observation,
            reference,
            target_conversation_id,
            target_conversation_type,
        )
        session.add(
            EventLink(
                from_source_event_id=observation.source_event_id,
                from_observation_id=observation.id,
                to_source_event_id=to_source_event_id,
                relation_type=reference.type,
                target_source_event_id=reference.source_event_id,
                target_platform_message_id=reference.platform_message_id,
                target_conversation_id=target_conversation_id,
                target_conversation_type=target_conversation_type,
                target_sender_id=reference.sender_id,
                confidence=100 if to_source_event_id is not None else None,
                resolver_status=resolver_status,
                raw_json=sanitize_payload(reference.raw, _metadata_policy(settings)) or {},
            )
        )


async def ingest_event(
    session: AsyncSession,
    payload: EventIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[EventObservation, bool]:
    existing = await _existing_observation(
        session,
        payload.instance.instance_id,
        idempotency_key,
        payload.source_event_id,
    )
    if existing:
        return existing, True

    fingerprint = event_correlation_fingerprint(payload)
    conversation_id = canonical_conversation_id(
        payload.instance.platform,
        payload.conversation.type,
        payload.conversation.id,
    )
    async with _correlation_guard(session, fingerprint):
        existing = await _existing_observation(
            session,
            payload.instance.instance_id,
            idempotency_key,
            payload.source_event_id,
        )
        if existing:
            return existing, True

        instance = await ensure_instance(session, payload.instance, settings)
        source = await ensure_source_event(
            session,
            payload,
            fingerprint,
            conversation_id,
            settings.correlation_window_seconds,
        )

        metadata = sanitize_payload(payload.metadata, _metadata_policy(settings)) or {}
        record = EventObservation(
            source_event_id=source.id,
            reported_source_event_id=payload.source_event_id,
            platform_message_id=payload.message.id if payload.message else None,
            instance_id=instance.id,
            idempotency_key=idempotency_key,
            adapter=payload.instance.adapter,
            bot_id=payload.instance.bot_id,
            conversation_name=payload.conversation.name,
            sender_id=payload.sender.id if payload.sender else None,
            sender_name=payload.sender.name if payload.sender else None,
            sender_roles_json=payload.sender.roles if payload.sender else [],
            text=payload.message.text if payload.message else None,
            segments_json=_dump_list(payload.message.segments if payload.message else [], settings),
            attachments_json=_dump_list(payload.message.attachments if payload.message else [], settings),
            raw_json=sanitize_payload(payload.raw, _raw_policy(settings)),
            metadata_json=metadata,
        )
        session.add(record)
        instance.last_event_at = payload.occurred_at
        try:
            await session.flush()
            await record_event_links(session, payload, record, settings)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            duplicate = await _existing_observation(
                session,
                payload.instance.instance_id,
                idempotency_key,
                payload.source_event_id,
            )
            if duplicate:
                return duplicate, True
            raise
    await session.refresh(record)
    return record, False


async def ingest_response(
    session: AsyncSession,
    payload: ResponseIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[ResponseRecord, bool]:
    existing = await session.scalar(
        select(ResponseRecord).where(
            ResponseRecord.instance_id == payload.instance.instance_id,
            ResponseRecord.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing, True

    if payload.trigger_observation_id and not await session.get(EventObservation, payload.trigger_observation_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="trigger_observation_id does not exist",
        )
    instance = await ensure_instance(session, payload.instance, settings)
    trigger_source_event_id = payload.trigger_source_event_id
    if trigger_source_event_id:
        trigger_observation = await session.scalar(
            select(EventObservation).where(
                EventObservation.instance_id == payload.instance.instance_id,
                EventObservation.reported_source_event_id == trigger_source_event_id,
            )
        )
        if trigger_observation is not None:
            trigger_source_event_id = trigger_observation.source_event_id
    record = ResponseRecord(
        source_response_id=payload.source_response_id,
        instance_id=instance.id,
        idempotency_key=idempotency_key,
        trigger_observation_id=payload.trigger_observation_id,
        trigger_source_event_id=trigger_source_event_id,
        trace_id=payload.trace_id,
        response_type=payload.response_type,
        platform=payload.instance.platform,
        adapter=payload.instance.adapter,
        bot_id=payload.instance.bot_id,
        conversation_id=payload.conversation.id,
        conversation_type=payload.conversation.type,
        platform_message_id=payload.platform_message_id,
        reply_to_platform_message_id=payload.reply_to_platform_message_id,
        text=payload.text,
        segments_json=_dump_list(payload.segments, settings),
        attachments_json=_dump_list(payload.attachments, settings),
        success=payload.success,
        error=payload.error,
        latency_ms=payload.latency_ms,
        raw_json=sanitize_payload(payload.raw, _raw_policy(settings)),
        metadata_json=sanitize_payload(payload.metadata, _metadata_policy(settings)) or {},
        occurred_at=payload.occurred_at,
    )
    session.add(record)
    instance.last_response_at = payload.occurred_at
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.scalar(
            select(ResponseRecord).where(
                ResponseRecord.instance_id == payload.instance.instance_id,
                ResponseRecord.idempotency_key == idempotency_key,
            )
        )
        if duplicate:
            return duplicate, True
        raise
    await session.refresh(record)
    return record, False


async def ingest_heartbeat(
    session: AsyncSession,
    payload: HeartbeatIn,
    settings: Settings,
) -> BotInstance:
    instance = await ensure_instance(session, payload.instance, settings)
    previous = instance.reported_status
    current = _reported_status(payload)
    instance.reported_status = current
    instance.last_heartbeat_at = payload.occurred_at
    instance.last_event_at = payload.last_event_at or instance.last_event_at
    instance.metadata_json = sanitize_payload(payload.metadata, _metadata_policy(settings)) or {}
    if current != previous:
        session.add(
            InstanceStatusTransition(
                instance_id=instance.id,
                previous_status=previous,
                status=current,
                detail_json={
                    "process_status": payload.process_status,
                    "connection_status": payload.connection_status,
                    "error_summary": payload.error_summary,
                },
            )
        )
    await session.commit()
    await session.refresh(instance)
    return instance


def effective_status(instance: BotInstance, stale_after_seconds: int) -> str:
    if instance.last_heartbeat_at is None:
        return "unknown"
    heartbeat = instance.last_heartbeat_at
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    if age > stale_after_seconds:
        return "offline"
    return instance.reported_status
