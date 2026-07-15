import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import threading
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import exists, func, or_, select, text as sql_text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    CommandRegistrySnapshotIn,
    EventIn,
    EventReference,
    HeartbeatIn,
    ResponseIn,
    SanitizationPolicy,
    sanitize_payload,
)

from .command_registry import (
    CommandRegistry,
    load_command_registry,
    match_runtime_candidates,
    runtime_match_supports_command,
    runtime_registry_snapshot_hash,
)
from .claims import ClaimEvaluation, enforcement_enabled, evaluate_claim
from .correlation import (
    CORRELATION_VERSION,
    advisory_lock_key,
    canonical_conversation_id,
    canonical_event_type,
    event_correlation_fingerprint,
)
from .decisions import POLICY_VERSION, decide_event
from .models import (
    BotInstance,
    CommandRegistrySnapshot,
    EventClaim,
    EventDecision,
    EventLink,
    EventObservation,
    InstanceStatusTransition,
    ResponseRecord,
    SourceEvent,
    utc_now,
)
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


def _native_identity_time(metadata: dict[str, Any]) -> str | None:
    native = metadata.get("native_identity")
    if not isinstance(native, dict):
        return None
    value = native.get("time")
    if value is None or isinstance(value, (dict, list, tuple, set, bytes, bytearray, bool)):
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return str(int(float(normalized)))
    except ValueError:
        return normalized


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
) -> tuple[SourceEvent, str]:
    compatible: list[SourceEvent] = []
    time_conflicts = 0
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
                .limit(10)
            )
        ).all()
        native_time = _native_identity_time(payload.metadata)
        for candidate in candidates:
            candidate_observations = (
                await session.scalars(
                    select(EventObservation).where(EventObservation.source_event_id == candidate.id)
                )
            ).all()
            candidate_times = {
                value
                for item in candidate_observations
                if (value := _native_identity_time(item.metadata_json)) is not None
            }
            if len(candidate_times) > 1 or (
                native_time is not None and candidate_times and candidate_times != {native_time}
            ):
                time_conflicts += 1
                continue
            compatible.append(candidate)
        if len(compatible) == 1:
            return compatible[0], "merged_strong_identity"

    source = SourceEvent(
        id=f"event:{uuid4()}",
        platform=payload.instance.platform,
        event_type=canonical_event_type(payload.instance.platform, payload.event_type),
        conversation_id=conversation_id,
        conversation_type=payload.conversation.type,
        message_id=payload.message.id if payload.message else None,
        correlation_fingerprint=fingerprint,
        correlation_version=CORRELATION_VERSION if fingerprint is not None else None,
        occurred_at=payload.occurred_at,
        first_received_at=utc_now(),
    )
    session.add(source)
    await session.flush()
    if fingerprint is None:
        correlation_status = "missing_or_inconsistent_strong_identity"
    elif time_conflicts:
        correlation_status = "native_time_conflict"
    elif len(compatible) > 1:
        correlation_status = "ambiguous_strong_identity"
    else:
        correlation_status = "new_strong_identity"
    return source, correlation_status


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
        source = await session.scalar(
            select(SourceEvent).where(
                SourceEvent.id == reference.source_event_id,
                SourceEvent.platform == payload.instance.platform,
                SourceEvent.conversation_id == target_conversation_id,
                SourceEvent.conversation_type == target_conversation_type,
                SourceEvent.occurred_at <= payload.occurred_at,
            )
        )
        if source is not None:
            return source.id, "resolved"

        candidates = (
            await session.scalars(
                select(EventObservation.source_event_id)
                .distinct()
                .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
                .where(
                    EventObservation.instance_id == payload.instance.instance_id,
                    EventObservation.reported_source_event_id == reference.source_event_id,
                    SourceEvent.platform == payload.instance.platform,
                    SourceEvent.conversation_id == target_conversation_id,
                    SourceEvent.conversation_type == target_conversation_type,
                    SourceEvent.occurred_at <= payload.occurred_at,
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
                .group_by(EventObservation.source_event_id)
                .order_by(func.max(SourceEvent.occurred_at).desc())
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


@dataclass(frozen=True, slots=True)
class _ReplyContext:
    has_reply_link: bool
    target_instance_id: str | None
    status: str
    target_sender_ids: frozenset[str]


def _segment_target_id(segment: dict[str, Any]) -> str | None:
    if segment.get("type") != "at":
        return None
    data = segment.get("data")
    if not isinstance(data, dict):
        data = segment
    value = data.get("qq") or data.get("target") or data.get("target_platform_userid")
    return str(value) if value is not None else None


async def _reply_context(
    session: AsyncSession,
    source: SourceEvent,
    bot_id_to_instance: dict[str, str],
) -> _ReplyContext:
    links = (
        await session.scalars(
            select(EventLink).where(
                EventLink.from_source_event_id == source.id,
                EventLink.relation_type == "reply_to",
            )
        )
    ).all()
    if not links:
        return _ReplyContext(False, None, "none", frozenset())

    target_instances: set[str] = set()
    target_sender_ids: set[str] = {item.target_sender_id for item in links if item.target_sender_id}
    resolved_other = False
    has_ambiguous = any(item.resolver_status == "ambiguous" for item in links)

    target_source_ids = {item.to_source_event_id for item in links if item.to_source_event_id}
    if target_source_ids:
        target_observations = (
            await session.scalars(
                select(EventObservation).where(EventObservation.source_event_id.in_(target_source_ids))
            )
        ).all()
        for target in target_observations:
            if not target.sender_id:
                continue
            target_sender_ids.add(target.sender_id)
            target_instance = bot_id_to_instance.get(target.sender_id)
            if target_instance is not None:
                target_instances.add(target_instance)
            else:
                resolved_other = True

    from_observation_ids = {item.from_observation_id for item in links}
    from_observations = (
        await session.scalars(
            select(EventObservation).where(EventObservation.id.in_(from_observation_ids))
        )
    ).all()
    link_instance_by_observation = {item.id: item.instance_id for item in from_observations}
    target_message_keys = {
        (link_instance_by_observation.get(item.from_observation_id), item.target_platform_message_id)
        for item in links
        if item.target_platform_message_id and link_instance_by_observation.get(item.from_observation_id)
    }
    target_message_ids = {message_id for _, message_id in target_message_keys}
    if target_message_ids:
        responses = (
            await session.scalars(
                select(ResponseRecord).where(
                    ResponseRecord.platform == source.platform,
                    ResponseRecord.conversation_type == source.conversation_type,
                    ResponseRecord.platform_message_id.in_(target_message_ids),
                    ResponseRecord.success.is_(True),
                )
            )
        ).all()
        for response in responses:
            if (response.instance_id, response.platform_message_id) not in target_message_keys:
                continue
            response_conversation_id = canonical_conversation_id(
                response.platform,
                response.conversation_type,
                response.conversation_id,
            )
            if response_conversation_id != source.conversation_id:
                continue
            target_instances.add(response.instance_id)
            target_sender_ids.add(response.bot_id)

    if len(target_instances) > 1:
        return _ReplyContext(True, None, "conflict", frozenset(target_sender_ids))
    if target_instances:
        return _ReplyContext(
            True,
            next(iter(target_instances)),
            "resolved_bot",
            frozenset(target_sender_ids),
        )
    if has_ambiguous:
        return _ReplyContext(True, None, "ambiguous", frozenset(target_sender_ids))
    if resolved_other or any(item.resolver_status == "resolved" for item in links):
        return _ReplyContext(True, None, "resolved_other", frozenset(target_sender_ids))
    return _ReplyContext(True, None, "unresolved", frozenset(target_sender_ids))


async def _runtime_filtered_registry(
    session: AsyncSession,
    registry: CommandRegistry,
    settings: Settings,
    text: str | None,
) -> tuple[CommandRegistry, dict[str, Any]]:
    snapshot = await session.scalar(
        select(CommandRegistrySnapshot)
        .where(CommandRegistrySnapshot.instance_id == "lily-command")
        .order_by(CommandRegistrySnapshot.received_at.desc())
        .limit(1)
    )
    if snapshot is None:
        return registry, {
            "status": "missing",
            "snapshot_hash": None,
            "static_rules": len(registry.rules),
            "active_rules": len(registry.rules),
            "runtime_match": None,
            "unregistered_match": None,
        }
    received_at = snapshot.received_at
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    age_seconds = max(0, int((utc_now() - received_at).total_seconds()))
    if age_seconds > settings.command_registry_snapshot_stale_seconds:
        return registry, {
            "status": "stale",
            "snapshot_hash": snapshot.snapshot_hash,
            "age_seconds": age_seconds,
            "static_rules": len(registry.rules),
            "active_rules": len(registry.rules),
            "runtime_match": None,
            "unregistered_match": None,
        }
    active = registry.active_for_runtime_plugins(snapshot.plugins_json)
    static_match = active.match(text)
    runtime_matches = match_runtime_candidates(snapshot.candidates_json, text)
    reviewed_matches = [
        item
        for item in runtime_matches
        if static_match is not None and runtime_match_supports_command(item, static_match)
    ]
    unregistered_matches = [item for item in runtime_matches if item not in reviewed_matches]
    return active, {
        "status": "fresh",
        "snapshot_hash": snapshot.snapshot_hash,
        "age_seconds": age_seconds,
        "static_rules": len(registry.rules),
        "active_rules": len(active.rules),
        "runtime_match": reviewed_matches[0] if reviewed_matches else None,
        "runtime_matches": runtime_matches,
        "unregistered_match": unregistered_matches[0] if unregistered_matches else None,
    }


async def recompute_event_decision(
    session: AsyncSession,
    source: SourceEvent,
    settings: Settings,
) -> EventDecision | None:
    """Serialize every recomputation path for one canonical source.

    Event ingestion is already protected by the correlation fingerprint, but
    response arrival and reference backfill can independently recompute the
    same decision.  A source-specific lock prevents those paths from losing a
    revision or overwriting newer aggregate features.
    """

    lock_fingerprint = hashlib.sha256(f"decision:{source.id}".encode()).hexdigest()
    async with _correlation_guard(session, lock_fingerprint):
        return await _recompute_event_decision_unlocked(session, source, settings)


async def _recompute_event_decision_unlocked(
    session: AsyncSession,
    source: SourceEvent,
    settings: Settings,
) -> EventDecision | None:
    observations = (
        await session.scalars(
            select(EventObservation)
            .where(EventObservation.source_event_id == source.id)
            .order_by(EventObservation.received_at, EventObservation.id)
        )
    ).all()
    if not observations:
        return None

    instances = (await session.scalars(select(BotInstance))).all()
    bot_id_to_instance = {str(item.bot_id): item.id for item in instances}
    reply_context = await _reply_context(session, source, bot_id_to_instance)

    mentioned_platform_ids = {
        target
        for observation in observations
        for segment in observation.segments_json
        if (target := _segment_target_id(segment)) is not None
    }
    if reply_context.has_reply_link:
        if reply_context.target_sender_ids:
            mentioned_platform_ids.difference_update(reply_context.target_sender_ids)
        elif reply_context.status in {"unresolved", "ambiguous", "conflict"}:
            mentioned_platform_ids = set()
    mentioned_bot_instances = {
        bot_id_to_instance[target]
        for target in mentioned_platform_ids
        if target in bot_id_to_instance
    }

    ordered_observations = sorted(
        observations,
        key=lambda item: (0 if item.instance_id == "lily-command" else 1, item.received_at, item.id),
    )
    preferred = next(
        (item for item in ordered_observations if item.instance_id == "lily-command" and item.text),
        next((item for item in ordered_observations if item.text), ordered_observations[0]),
    )
    aggregate_metadata = {
        "to_me": any(bool(item.metadata_json.get("to_me")) for item in observations),
        "is_tome": any(bool(item.metadata_json.get("is_tome")) for item in observations),
    }
    aggregate_attachments = [attachment for item in observations for attachment in item.attachments_json]

    registry_error = None
    try:
        command_registry = load_command_registry(settings.command_registry_path)
    except (OSError, ValueError) as exc:
        command_registry = CommandRegistry.empty()
        registry_error = f"{type(exc).__name__}: {exc}"
    command_registry, registry_runtime = await _runtime_filtered_registry(
        session,
        command_registry,
        settings,
        preferred.text,
    )
    decision = decide_event(
        source_event_type=source.event_type,
        conversation_type=source.conversation_type,
        text=preferred.text,
        attachments=aggregate_attachments,
        metadata=aggregate_metadata,
        has_reply_link=reply_context.has_reply_link,
        reply_target_instance_id=reply_context.target_instance_id,
        reply_target_status=reply_context.status,
        mentioned_bot_instance_ids=sorted(mentioned_bot_instances),
        observation_count=len(observations),
        command_registry=command_registry,
        command_registry_error=registry_error,
        command_registry_runtime=registry_runtime,
        sender_bot_instance_id=(
            bot_id_to_instance.get(str(preferred.sender_id))
            if preferred.sender_id is not None
            else None
        ),
        conversation_mode=settings.conversation_mode(
            source.platform,
            source.conversation_type,
            source.conversation_id,
        ),
    )
    features = {
        **decision.features,
        "deciding_instance_id": preferred.instance_id,
        "observation_ids": [item.id for item in ordered_observations],
        "recomputed_at": utc_now().isoformat(),
    }
    record = await session.scalar(select(EventDecision).where(EventDecision.source_event_id == source.id))
    if record is None:
        record = EventDecision(
            source_event_id=source.id,
            deciding_observation_id=preferred.id,
            policy_version=POLICY_VERSION,
            decision_type=decision.decision_type,
            target_instance_id=decision.target_instance_id,
            confidence=decision.confidence,
            reason=decision.reason,
            features_json=features,
            revision=1,
            updated_at=utc_now(),
        )
        session.add(record)
        return record

    record.deciding_observation_id = preferred.id
    record.policy_version = POLICY_VERSION
    record.decision_type = decision.decision_type
    record.target_instance_id = decision.target_instance_id
    record.confidence = decision.confidence
    record.reason = decision.reason
    record.features_json = features
    record.revision += 1
    record.updated_at = utc_now()
    return record


async def _recompute_decisions_for_response(
    session: AsyncSession,
    response: ResponseRecord,
    settings: Settings,
) -> None:
    if not response.platform_message_id:
        return
    links = (
        await session.scalars(
            select(EventLink)
            .join(EventObservation, EventObservation.id == EventLink.from_observation_id)
            .where(
                EventLink.relation_type == "reply_to",
                EventLink.target_platform_message_id == response.platform_message_id,
                EventLink.target_conversation_type == response.conversation_type,
                EventObservation.instance_id == response.instance_id,
            )
        )
    ).all()
    source_ids: set[str] = set()
    response_conversation_id = canonical_conversation_id(
        response.platform,
        response.conversation_type,
        response.conversation_id,
    )
    for link in links:
        link_conversation_id = canonical_conversation_id(
            response.platform,
            link.target_conversation_type or response.conversation_type,
            link.target_conversation_id or response.conversation_id,
        )
        if link_conversation_id == response_conversation_id:
            source_ids.add(link.from_source_event_id)
    for source_id in sorted(source_ids):
        source = await session.get(SourceEvent, source_id)
        if source is not None:
            await recompute_event_decision(session, source, settings)


async def _resolve_stored_link(session: AsyncSession, link: EventLink) -> str:
    from_observation = await session.get(EventObservation, link.from_observation_id)
    from_source = await session.get(SourceEvent, link.from_source_event_id)
    if from_observation is None or from_source is None:
        return "unresolved"

    if link.target_source_event_id:
        target_source = await session.scalar(
            select(SourceEvent).where(
                SourceEvent.id == link.target_source_event_id,
                SourceEvent.platform == from_source.platform,
                SourceEvent.conversation_id == link.target_conversation_id,
                SourceEvent.conversation_type == link.target_conversation_type,
                SourceEvent.occurred_at <= from_source.occurred_at,
            )
        )
        if target_source is not None:
            link.to_source_event_id = target_source.id
            link.resolver_status = "resolved"
            link.confidence = 100
            return "resolved"
        reported_candidates = (
            await session.scalars(
                select(EventObservation.source_event_id)
                .distinct()
                .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
                .where(
                    EventObservation.instance_id == from_observation.instance_id,
                    EventObservation.reported_source_event_id == link.target_source_event_id,
                    SourceEvent.platform == from_source.platform,
                    SourceEvent.conversation_id == link.target_conversation_id,
                    SourceEvent.conversation_type == link.target_conversation_type,
                    SourceEvent.occurred_at <= from_source.occurred_at,
                )
                .limit(2)
            )
        ).all()
        if len(reported_candidates) == 1:
            link.to_source_event_id = reported_candidates[0]
            link.resolver_status = "resolved"
            link.confidence = 100
            return "resolved"
        if len(reported_candidates) > 1:
            link.to_source_event_id = None
            link.resolver_status = "ambiguous"
            link.confidence = None
            return "ambiguous"

    if link.target_platform_message_id:
        candidates = (
            await session.scalars(
                select(EventObservation.source_event_id)
                .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
                .where(
                    EventObservation.id != from_observation.id,
                    EventObservation.instance_id == from_observation.instance_id,
                    EventObservation.platform_message_id == link.target_platform_message_id,
                    SourceEvent.platform == from_source.platform,
                    SourceEvent.conversation_id == link.target_conversation_id,
                    SourceEvent.conversation_type == link.target_conversation_type,
                    SourceEvent.occurred_at <= from_source.occurred_at,
                )
                .group_by(EventObservation.source_event_id)
                .order_by(func.max(SourceEvent.occurred_at).desc())
                .limit(2)
            )
        ).all()
        if len(candidates) == 1:
            link.to_source_event_id = candidates[0]
            link.resolver_status = "resolved"
            link.confidence = 100
            return "resolved"
        if len(candidates) > 1:
            link.to_source_event_id = None
            link.resolver_status = "ambiguous"
            link.confidence = None
            return "ambiguous"

    link.to_source_event_id = None
    link.resolver_status = "unresolved"
    link.confidence = None
    return "unresolved"


async def _recompute_sources_for_links(
    session: AsyncSession,
    source_ids: set[str],
    settings: Settings,
) -> None:
    for source_id in sorted(source_ids):
        source = await session.get(SourceEvent, source_id)
        if source is not None:
            await recompute_event_decision(session, source, settings)


async def resolve_pending_links(
    session: AsyncSession,
    settings: Settings,
    limit: int = 5000,
) -> dict[str, int]:
    links = (
        await session.scalars(
            select(EventLink)
            .where(EventLink.resolver_status.in_(("unresolved", "ambiguous")))
            .order_by(EventLink.created_at, EventLink.id)
            .limit(limit)
        )
    ).all()
    counts = {"examined": len(links), "resolved": 0, "ambiguous": 0, "unresolved": 0}
    affected_sources: set[str] = set()
    for link in links:
        previous = link.resolver_status
        result = await _resolve_stored_link(session, link)
        counts[result] += 1
        if result != previous or result == "resolved":
            affected_sources.add(link.from_source_event_id)
    await session.flush()
    await _recompute_sources_for_links(session, affected_sources, settings)
    await session.commit()
    return counts


async def _resolve_links_targeting_observation(
    session: AsyncSession,
    observation: EventObservation,
    source: SourceEvent,
    settings: Settings,
) -> None:
    if not observation.platform_message_id:
        return
    candidates = (
        await session.scalars(
            select(EventLink)
            .join(EventObservation, EventObservation.id == EventLink.from_observation_id)
            .where(
                EventLink.resolver_status.in_(("unresolved", "ambiguous")),
                EventLink.target_platform_message_id == observation.platform_message_id,
                EventLink.target_conversation_id == source.conversation_id,
                EventLink.target_conversation_type == source.conversation_type,
                EventObservation.instance_id == observation.instance_id,
            )
            .order_by(EventLink.created_at, EventLink.id)
            .limit(100)
        )
    ).all()
    affected_sources: set[str] = set()
    for link in candidates:
        if await _resolve_stored_link(session, link) == "resolved":
            affected_sources.add(link.from_source_event_id)
    if affected_sources:
        await session.flush()
        await _recompute_sources_for_links(session, affected_sources, settings)


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
        source, correlation_status = await ensure_source_event(
            session,
            payload,
            fingerprint,
            conversation_id,
            settings.correlation_window_seconds,
        )

        metadata = sanitize_payload(payload.metadata, _metadata_policy(settings)) or {}
        metadata["correlation"] = {
            "version": CORRELATION_VERSION,
            "status": correlation_status,
            "fingerprint_present": fingerprint is not None,
        }
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
            await session.execute(
                update(ResponseRecord)
                .where(
                    ResponseRecord.instance_id == record.instance_id,
                    ResponseRecord.trigger_source_event_id == payload.source_event_id,
                )
                .values(trigger_source_event_id=source.id)
            )
            await record_event_links(session, payload, record, settings)
            await session.flush()
            await recompute_event_decision(session, source, settings)
            await _resolve_links_targeting_observation(session, record, source, settings)
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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
        await session.flush()
        await _recompute_decisions_for_response(session, record, settings)
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
    received_at = utc_now()
    heartbeat_at = min(received_at, payload.occurred_at)
    existing = await session.get(BotInstance, payload.instance.instance_id)
    previous_heartbeat = existing.last_heartbeat_at if existing is not None else None
    if existing is not None and previous_heartbeat is not None:
        if previous_heartbeat.tzinfo is None:
            previous_heartbeat = previous_heartbeat.replace(tzinfo=timezone.utc)
        if heartbeat_at < previous_heartbeat:
            await session.commit()
            await session.refresh(existing)
            return existing
    instance = await ensure_instance(session, payload.instance, settings)
    previous = instance.reported_status
    current = _reported_status(payload)
    instance.reported_status = current
    instance.last_heartbeat_at = heartbeat_at
    instance.last_event_at = payload.last_event_at or instance.last_event_at
    metadata = sanitize_payload(payload.metadata, _metadata_policy(settings)) or {}
    metadata["heartbeat_reported_at"] = payload.occurred_at.isoformat()
    if payload.capabilities is not None:
        metadata["capabilities"] = payload.capabilities.model_dump(mode="json")
    instance.metadata_json = metadata
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


async def ingest_command_registry_snapshot(
    session: AsyncSession,
    payload: CommandRegistrySnapshotIn,
    settings: Settings,
) -> tuple[CommandRegistrySnapshot, bool]:
    plugins = [item.model_dump(mode="json") for item in payload.plugins]
    candidates = [item.model_dump(mode="json") for item in payload.candidates]
    if runtime_registry_snapshot_hash(plugins, candidates) != payload.snapshot_hash:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="snapshot_hash does not match plugins and candidates",
        )
    existing = await session.scalar(
        select(CommandRegistrySnapshot).where(
            CommandRegistrySnapshot.instance_id == payload.instance.instance_id,
            CommandRegistrySnapshot.snapshot_hash == payload.snapshot_hash,
        )
    )
    if existing is not None:
        existing.observed_at = payload.observed_at
        existing.received_at = utc_now()
        await session.commit()
        await session.refresh(existing)
        return existing, True

    await ensure_instance(session, payload.instance, settings)
    record = CommandRegistrySnapshot(
        instance_id=payload.instance.instance_id,
        snapshot_hash=payload.snapshot_hash,
        observed_at=payload.observed_at,
        plugins_json=plugins,
        candidates_json=candidates,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.scalar(
            select(CommandRegistrySnapshot).where(
                CommandRegistrySnapshot.instance_id == payload.instance.instance_id,
                CommandRegistrySnapshot.snapshot_hash == payload.snapshot_hash,
            )
        )
        if duplicate is not None:
            duplicate.observed_at = payload.observed_at
            duplicate.received_at = utc_now()
            await session.commit()
            await session.refresh(duplicate)
            return duplicate, True
        raise
    await session.refresh(record)
    return record, False


async def _claim_observation_count(
    session: AsyncSession,
    source_event_id: str,
) -> int:
    count = int(
        await session.scalar(
            select(func.count(EventObservation.id)).where(EventObservation.source_event_id == source_event_id)
        )
        or 0
    )
    # End the short read transaction so SQLite tests and PostgreSQL READ COMMITTED
    # polling can observe the other bridge's concurrently committed observation.
    await session.commit()
    return count


async def _coalesce_claim_observations(
    session: AsyncSession,
    source_event_id: str,
    required_observations: int,
    wait_milliseconds: int,
) -> int:
    count = await _claim_observation_count(session, source_event_id)
    if count >= required_observations or wait_milliseconds <= 0:
        return count
    deadline = asyncio.get_running_loop().time() + wait_milliseconds / 1000
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(min(0.02, max(0.001, wait_milliseconds / 1000 / 10)))
        count = await _claim_observation_count(session, source_event_id)
        if count >= required_observations:
            return count
    return count


async def _ensure_claim_decision(
    session: AsyncSession,
    source: SourceEvent,
    settings: Settings,
) -> EventDecision | None:
    decision = await session.scalar(
        select(EventDecision)
        .where(EventDecision.source_event_id == source.id)
        .execution_options(populate_existing=True)
    )
    if decision is not None:
        return decision

    decision = await recompute_event_decision(session, source, settings)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == source.id)
        )
    return decision


def claim_record_payload(record: EventClaim) -> dict[str, Any]:
    return {
        "claim_id": record.id,
        "source_event_id": record.source_event_id,
        "instance_id": record.instance_id,
        "decision_id": record.decision_id,
        "decision_revision": record.decision_revision,
        "mode": record.mode,
        "action": record.action,
        "reason": record.reason,
        "ready": record.ready,
        "enforced": record.enforced,
        "features": record.features_json,
        "created_at": record.created_at,
    }


async def evaluate_event_claim(
    session: AsyncSession,
    payload: EventIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[EventClaim, bool]:
    existing = await session.scalar(
        select(EventClaim).where(
            EventClaim.instance_id == payload.instance.instance_id,
            EventClaim.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        return existing, True

    observation, _ = await ingest_event(session, payload, idempotency_key, settings)
    source = await session.get(SourceEvent, observation.source_event_id)
    assert source is not None

    observation_count = await _coalesce_claim_observations(
        session,
        source.id,
        settings.claim_required_observations,
        settings.claim_coalesce_milliseconds if settings.claim_mode != "off" else 0,
    )
    decision = await _ensure_claim_decision(session, source, settings)

    if decision is None:
        evaluation = ClaimEvaluation(
            action="abstain",
            reason="decision_unavailable",
            ready=False,
            gates={"observation_count": observation_count},
        )
        target_status = None
    else:
        target_status = None
        if decision.target_instance_id:
            target = await session.get(BotInstance, decision.target_instance_id, populate_existing=True)
            if target is not None:
                target_status = effective_status(target, settings.stale_after_seconds)
        evaluation = evaluate_claim(
            mode=settings.claim_mode,
            requesting_instance_id=payload.instance.instance_id,
            decision_type=decision.decision_type,
            target_instance_id=decision.target_instance_id,
            confidence=decision.confidence,
            decision_features=decision.features_json,
            correlation_version=source.correlation_version,
            observation_count=observation_count,
            required_observations=settings.claim_required_observations,
            minimum_confidence=settings.claim_minimum_confidence,
            target_status=target_status,
        )

    configured_enforcement = enforcement_enabled(
        mode=settings.claim_mode,
        platform=source.platform,
        conversation_type=source.conversation_type,
        conversation_id=source.conversation_id,
        canary_conversations=settings.claim_canary_conversations,
    )
    action = evaluation.action
    reason = evaluation.reason
    ready = evaluation.ready
    enforced = configured_enforcement and ready and action in {"allow", "deny"}
    lock_fingerprint = hashlib.sha256(f"claim:{source.id}".encode()).hexdigest()
    coordination: dict[str, Any] = {
        "observed_peer_instance_ids": [],
        "enforced_deny_instance_ids": [],
    }

    async with _correlation_guard(session, lock_fingerprint):
        existing = await session.scalar(
            select(EventClaim).where(
                EventClaim.source_event_id == source.id,
                EventClaim.instance_id == payload.instance.instance_id,
            )
        )
        if existing is not None:
            await session.commit()
            return existing, True

        # An allow is only an exclusive owner after every other instance that
        # observed this canonical event has already received an enforced deny.
        # Without this handshake, a late second observation could grant allow
        # after the first bridge had timed out, failed open, and continued its
        # legacy matcher path.  Deny may stand alone safely; allow may not.
        if enforced and action == "allow":
            observed_peer_instance_ids = set(
                (
                    await session.scalars(
                        select(EventObservation.instance_id)
                        .where(
                            EventObservation.source_event_id == source.id,
                            EventObservation.instance_id != payload.instance.instance_id,
                        )
                        .distinct()
                    )
                ).all()
            )
            enforced_deny_instance_ids = set(
                (
                    await session.scalars(
                        select(EventClaim.instance_id).where(
                            EventClaim.source_event_id == source.id,
                            EventClaim.instance_id.in_(observed_peer_instance_ids),
                            EventClaim.action == "deny",
                            EventClaim.enforced.is_(True),
                        )
                    )
                ).all()
            )
            coordination = {
                "observed_peer_instance_ids": sorted(observed_peer_instance_ids),
                "enforced_deny_instance_ids": sorted(enforced_deny_instance_ids),
            }
            if not observed_peer_instance_ids or enforced_deny_instance_ids != observed_peer_instance_ids:
                action = "abstain"
                reason = "claim_peers_not_denied"
                ready = False
                enforced = False

        if enforced and action == "allow":
            owner = await session.scalar(
                select(EventClaim).where(
                    EventClaim.source_event_id == source.id,
                    EventClaim.action == "allow",
                    EventClaim.enforced.is_(True),
                )
            )
            if owner is not None and owner.instance_id != payload.instance.instance_id:
                action = "abstain"
                reason = f"claim_owner_conflict:{owner.instance_id}"
                ready = False
                enforced = False

        record = EventClaim(
            source_event_id=source.id,
            instance_id=payload.instance.instance_id,
            idempotency_key=idempotency_key,
            decision_id=decision.id if decision else None,
            decision_revision=decision.revision if decision else None,
            mode=settings.claim_mode,
            action=action,
            reason=reason,
            ready=ready,
            enforced=enforced,
            features_json={
                "gates": evaluation.gates,
                "configured_enforcement": configured_enforcement,
                "target_status": target_status,
                "coordination": coordination,
            },
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            duplicate = await session.scalar(
                select(EventClaim).where(
                    EventClaim.source_event_id == source.id,
                    EventClaim.instance_id == payload.instance.instance_id,
                )
            )
            if duplicate is not None:
                return duplicate, True
            raise
    await session.refresh(record)
    return record, False
