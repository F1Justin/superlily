from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn, HeartbeatIn, ResponseIn, SanitizationPolicy, sanitize_payload

from .models import BotInstance, EventObservation, InstanceStatusTransition, ResponseRecord, SourceEvent, utc_now
from .settings import Settings


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


async def ensure_source_event(session: AsyncSession, payload: EventIn) -> SourceEvent:
    values = {
        "id": payload.source_event_id,
        "platform": payload.instance.platform,
        "event_type": payload.event_type,
        "conversation_id": payload.conversation.id,
        "conversation_type": payload.conversation.type,
        "message_id": payload.message.id if payload.message else None,
        "occurred_at": payload.occurred_at,
        "first_received_at": utc_now(),
    }
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(SourceEvent).values(**values).on_conflict_do_nothing(
            index_elements=[SourceEvent.id]
        )
        await session.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(SourceEvent).values(**values).on_conflict_do_nothing(
            index_elements=[SourceEvent.id]
        )
        await session.execute(statement)
    else:
        source = await session.get(SourceEvent, payload.source_event_id)
        if source is None:
            source = SourceEvent(**values)
            session.add(source)
            await session.flush()
            return source
    source = await session.get(SourceEvent, payload.source_event_id, populate_existing=True)
    assert source is not None
    return source


async def ingest_event(
    session: AsyncSession,
    payload: EventIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[EventObservation, bool]:
    existing = await session.scalar(
        select(EventObservation).where(
            EventObservation.instance_id == payload.instance.instance_id,
            EventObservation.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return existing, True

    instance = await ensure_instance(session, payload.instance, settings)
    source = await ensure_source_event(session, payload)

    metadata = sanitize_payload(payload.metadata, _metadata_policy(settings)) or {}
    record = EventObservation(
        source_event_id=source.id,
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
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.scalar(
            select(EventObservation).where(
                EventObservation.instance_id == payload.instance.instance_id,
                EventObservation.idempotency_key == idempotency_key,
            )
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
    record = ResponseRecord(
        source_response_id=payload.source_response_id,
        instance_id=instance.id,
        idempotency_key=idempotency_key,
        trigger_observation_id=payload.trigger_observation_id,
        trigger_source_event_id=payload.trigger_source_event_id,
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
