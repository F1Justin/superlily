from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import CommandRegistrySnapshotIn, EventIn, HeartbeatIn, ResponseIn

from .audit import classify_decision_outcome
from .auth import ingest_identity, require_admin
from .command_registry import (
    load_command_registry,
    runtime_candidate_trigger_reviewed,
    runtime_plugin_aliases,
    source_plugin_loaded,
)
from .dependencies import get_session
from .models import (
    BotInstance,
    CommandRegistrySnapshot,
    EventClaim,
    EventDecision,
    EventLink,
    EventObservation,
    ResponseRecord,
    SourceEvent,
)
from .service import (
    claim_record_payload,
    effective_status,
    evaluate_event_claim,
    ingest_command_registry_snapshot,
    ingest_event,
    ingest_heartbeat,
    ingest_response,
    resolve_pending_links,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
Identity = Annotated[str, Depends(ingest_identity)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=256)]


def _verify_identity(authenticated_instance: str, payload_instance: str) -> None:
    if authenticated_instance != payload_instance:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not authorized for payload instance",
        )


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await request.app.state.database.ping()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok"}


@router.post("/v1/events", status_code=status.HTTP_201_CREATED)
async def post_event(
    payload: EventIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate = await ingest_event(session, payload, idempotency_key, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {"observation_id": record.id, "source_event_id": record.source_event_id, "duplicate": duplicate}


@router.post("/v1/claims/evaluate", status_code=status.HTTP_200_OK)
async def post_claim(
    payload: EventIn,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate = await evaluate_event_claim(
        session,
        payload,
        idempotency_key,
        session.info["settings"],
    )
    return {**claim_record_payload(record), "duplicate": duplicate}


@router.post("/v1/responses", status_code=status.HTTP_201_CREATED)
async def post_response(
    payload: ResponseIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate = await ingest_response(session, payload, idempotency_key, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {"response_id": record.id, "source_response_id": record.source_response_id, "duplicate": duplicate}


@router.post("/v1/heartbeats", status_code=status.HTTP_200_OK)
async def post_heartbeat(
    payload: HeartbeatIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record = await ingest_heartbeat(session, payload, session.info["settings"])
    return {"instance_id": record.id, "reported_status": record.reported_status}


@router.post("/v1/command-registry/snapshots", status_code=status.HTTP_201_CREATED)
async def post_command_registry_snapshot(
    payload: CommandRegistrySnapshotIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    if authenticated_instance != "lily-command":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only lily-command may publish the runtime command registry",
        )
    record, duplicate = await ingest_command_registry_snapshot(session, payload, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {"snapshot_id": record.id, "snapshot_hash": record.snapshot_hash, "duplicate": duplicate}


@router.get("/v1/events/recent", dependencies=[Depends(require_admin)])
async def recent_events(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventObservation, SourceEvent)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .order_by(desc(EventObservation.received_at))
            .limit(limit)
        )
    ).all()
    return [
        {
            "observation_id": observation.id,
            "source_event_id": source.id,
            "reported_source_event_id": observation.reported_source_event_id,
            "instance_id": observation.instance_id,
            "platform": source.platform,
            "adapter": observation.adapter,
            "event_type": source.event_type,
            "conversation": {
                "id": source.conversation_id,
                "type": source.conversation_type,
                "name": observation.conversation_name,
            },
            "sender": {"id": observation.sender_id, "name": observation.sender_name},
            "message_id": observation.platform_message_id,
            "native_identity": observation.metadata_json.get("native_identity"),
            "correlation_diagnostic": observation.metadata_json.get("correlation"),
            "correlation_version": source.correlation_version,
            "text": observation.text,
            "occurred_at": source.occurred_at,
            "received_at": observation.received_at,
        }
        for observation, source in rows
    ]


@router.get("/v1/responses/recent", dependencies=[Depends(require_admin)])
async def recent_responses(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.scalars(select(ResponseRecord).order_by(desc(ResponseRecord.received_at)).limit(limit))
    ).all()
    return [
        {
            "response_id": item.id,
            "source_response_id": item.source_response_id,
            "instance_id": item.instance_id,
            "trigger_observation_id": item.trigger_observation_id,
            "trigger_source_event_id": item.trigger_source_event_id,
            "conversation_id": item.conversation_id,
            "platform_message_id": item.platform_message_id,
            "response_type": item.response_type,
            "text": item.text,
            "success": item.success,
            "error": item.error,
            "latency_ms": item.latency_ms,
            "occurred_at": item.occurred_at,
            "received_at": item.received_at,
        }
        for item in rows
    ]


@router.get("/v1/event-links/recent", dependencies=[Depends(require_admin)])
async def recent_event_links(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventLink).order_by(desc(EventLink.created_at)).limit(limit))).all()
    return [
        {
            "link_id": item.id,
            "from_source_event_id": item.from_source_event_id,
            "from_observation_id": item.from_observation_id,
            "to_source_event_id": item.to_source_event_id,
            "relation_type": item.relation_type,
            "target_source_event_id": item.target_source_event_id,
            "target_platform_message_id": item.target_platform_message_id,
            "target_conversation_id": item.target_conversation_id,
            "target_conversation_type": item.target_conversation_type,
            "target_sender_id": item.target_sender_id,
            "confidence": item.confidence,
            "resolver_status": item.resolver_status,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.post("/v1/event-links/resolve", dependencies=[Depends(require_admin)])
async def resolve_event_links(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=5000)] = 5000,
) -> dict[str, int]:
    return await resolve_pending_links(session, session.info["settings"], limit)


@router.get("/v1/decisions/recent", dependencies=[Depends(require_admin)])
async def recent_decisions(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventDecision).order_by(desc(EventDecision.updated_at)).limit(limit))).all()
    return [
        {
            "decision_id": item.id,
            "source_event_id": item.source_event_id,
            "deciding_observation_id": item.deciding_observation_id,
            "policy_version": item.policy_version,
            "decision_type": item.decision_type,
            "target_instance_id": item.target_instance_id,
            "confidence": item.confidence,
            "reason": item.reason,
            "features": item.features_json,
            "revision": item.revision,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in rows
    ]


@router.get("/v1/claims/recent", dependencies=[Depends(require_admin)])
async def recent_claims(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventClaim).order_by(desc(EventClaim.created_at)).limit(limit))).all()
    return [claim_record_payload(item) for item in rows]


@router.get("/v1/claims/summary", dependencies=[Depends(require_admin)])
async def claim_summary(
    request: Request,
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await session.scalars(
            select(EventClaim).where(EventClaim.created_at >= since).order_by(EventClaim.created_at)
        )
    ).all()
    return {
        "since": since,
        "hours": hours,
        "mode": request.app.state.settings.claim_mode,
        "canary_conversations": sorted(request.app.state.settings.claim_canary_conversations),
        "claims": len(rows),
        "actions": dict(sorted(Counter(item.action for item in rows).items())),
        "enforced": dict(sorted(Counter(item.action for item in rows if item.enforced).items())),
        "reasons": dict(sorted(Counter(item.reason for item in rows).items())),
        "by_instance": dict(sorted(Counter(item.instance_id for item in rows).items())),
    }


def _compact_text(value: str | None, limit: int = 80) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@router.get("/v1/native-identities/recent", dependencies=[Depends(require_admin)])
async def recent_native_identities(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventObservation, SourceEvent)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .order_by(desc(EventObservation.received_at))
            .limit(min(limit * 5, 2500))
        )
    ).all()
    result = []
    for observation, source in rows:
        native_identity = observation.metadata_json.get("native_identity")
        if not isinstance(native_identity, dict) or not native_identity:
            continue
        text_preview = _compact_text(observation.text)
        conversation = f"{source.conversation_type}:{source.conversation_id}"
        summary = (
            f"{source.occurred_at.isoformat()} | {conversation} | {observation.instance_id} | "
            f"sender={observation.sender_id or '-'} | message_id={native_identity.get('message_id', '-')} | "
            f"real_seq={native_identity.get('real_seq', '-')} | {text_preview}"
        )
        result.append(
            {
                "summary": summary,
                "source_event_id": source.id,
                "observation_id": observation.id,
                "instance_id": observation.instance_id,
                "conversation": {
                    "id": source.conversation_id,
                    "type": source.conversation_type,
                    "display": conversation,
                },
                "sender_id": observation.sender_id,
                "text_preview": text_preview,
                "platform_message_id": observation.platform_message_id,
                "native_identity": native_identity,
                "occurred_at": source.occurred_at,
                "received_at": observation.received_at,
            }
        )
        if len(result) >= limit:
            break
    return result


@router.get("/v1/native-identities/coverage", dependencies=[Depends(require_admin)])
async def native_identity_coverage(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    observations = (
        await session.scalars(
            select(EventObservation)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .where(EventObservation.received_at >= since)
            .where(SourceEvent.event_type == "message")
            .order_by(desc(EventObservation.received_at))
        )
    ).all()
    by_instance: dict[str, dict] = {}
    for observation in observations:
        stats = by_instance.setdefault(
            observation.instance_id,
            {"observations": 0, "with_native_identity": 0, "fields": {}},
        )
        stats["observations"] += 1
        native_identity = observation.metadata_json.get("native_identity")
        if not isinstance(native_identity, dict) or not native_identity:
            continue
        stats["with_native_identity"] += 1
        for field in native_identity:
            if field == "schema":
                continue
            stats["fields"][field] = stats["fields"].get(field, 0) + 1

    instances = []
    for instance_id, stats in sorted(by_instance.items()):
        total = stats["observations"]
        captured = stats["with_native_identity"]
        instances.append(
            {
                "instance_id": instance_id,
                **stats,
                "coverage_percent": round(captured * 100 / total, 2) if total else 0.0,
            }
        )
    return {"since": since, "hours": hours, "observations": len(observations), "instances": instances}


@router.get("/v1/decisions/summary", dependencies=[Depends(require_admin)])
async def decision_summary(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventDecision, SourceEvent, EventObservation)
            .join(SourceEvent, SourceEvent.id == EventDecision.source_event_id)
            .join(EventObservation, EventObservation.id == EventDecision.deciding_observation_id, isouter=True)
            .order_by(desc(EventDecision.updated_at))
            .limit(limit)
        )
    ).all()
    result = []
    for decision, source, observation in rows:
        conversation = f"{source.conversation_type}:{source.conversation_id}"
        sender = None
        text = None
        instance_id = None
        if observation is not None:
            sender = observation.sender_name or observation.sender_id
            text = observation.text
            instance_id = observation.instance_id
        text_preview = _compact_text(text or decision.features_json.get("text_preview"))
        target = decision.target_instance_id or "-"
        sender_display = sender or "-"
        summary = (
            f"{decision.created_at.isoformat()} | {conversation} | {sender_display} | "
            f"{text_preview} | {decision.decision_type} -> {target} | {decision.reason}"
        )
        result.append(
            {
                "summary": summary,
                "created_at": decision.created_at,
                "updated_at": decision.updated_at,
                "revision": decision.revision,
                "conversation": {
                    "id": source.conversation_id,
                    "type": source.conversation_type,
                    "display": conversation,
                },
                "sender": sender,
                "text_preview": text_preview,
                "decision_type": decision.decision_type,
                "target_instance_id": decision.target_instance_id,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "source_event_id": decision.source_event_id,
                "observation_id": decision.deciding_observation_id,
                "instance_id": instance_id,
            }
        )
    return result


@router.get("/v1/decisions/outcomes", dependencies=[Depends(require_admin)])
async def decision_outcomes(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    grace_seconds: Annotated[int, Query(ge=0, le=3600)] = 30,
    detail_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    decision_rows = (
        await session.execute(
            select(EventDecision, SourceEvent, EventObservation)
            .join(SourceEvent, SourceEvent.id == EventDecision.source_event_id)
            .join(EventObservation, EventObservation.id == EventDecision.deciding_observation_id, isouter=True)
            .where(SourceEvent.first_received_at >= since)
            .order_by(desc(EventDecision.updated_at))
        )
    ).all()
    source_ids = {decision.source_event_id for decision, _, _ in decision_rows}
    responses = (
        await session.scalars(
            select(ResponseRecord)
            .where(ResponseRecord.received_at >= since)
            .order_by(ResponseRecord.received_at)
        )
    ).all()
    responses_by_source: dict[str, list[ResponseRecord]] = defaultdict(list)
    for response in responses:
        if response.trigger_source_event_id:
            responses_by_source[response.trigger_source_event_id].append(response)

    outcome_counts: Counter[str] = Counter()
    details = []
    for decision, source, observation in decision_rows:
        linked = responses_by_source.get(source.id, [])
        successful = {item.instance_id for item in linked if item.success}
        failed = {item.instance_id for item in linked if not item.success}
        first_received_at = source.first_received_at
        if first_received_at.tzinfo is None:
            first_received_at = first_received_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - first_received_at).total_seconds()))
        outcome = classify_decision_outcome(
            decision_type=decision.decision_type,
            target_instance_id=decision.target_instance_id,
            successful_instances=successful,
            failed_instances=failed,
            age_seconds=age_seconds,
            grace_seconds=grace_seconds,
        )
        outcome_counts[outcome] += 1
        if outcome not in {"matched", "matched_no_response"} and len(details) < detail_limit:
            details.append(
                {
                    "source_event_id": source.id,
                    "conversation": f"{source.conversation_type}:{source.conversation_id}",
                    "sender": observation.sender_name or observation.sender_id if observation else None,
                    "text_preview": _compact_text(observation.text if observation else None),
                    "decision_type": decision.decision_type,
                    "target_instance_id": decision.target_instance_id,
                    "reason": decision.reason,
                    "outcome": outcome,
                    "successful_instances": sorted(successful),
                    "failed_instances": sorted(failed),
                    "age_seconds": age_seconds,
                    "updated_at": decision.updated_at,
                }
            )

    linked_responses = [item for item in responses if item.trigger_source_event_id in source_ids]
    unlinked_responses = [item for item in responses if item.trigger_source_event_id is None]
    outside_window_responses = [
        item
        for item in responses
        if item.trigger_source_event_id is not None and item.trigger_source_event_id not in source_ids
    ]
    return {
        "since": since,
        "hours": hours,
        "grace_seconds": grace_seconds,
        "decisions": len(decision_rows),
        "outcomes": dict(sorted(outcome_counts.items())),
        "responses": {
            "total": len(responses),
            "linked": len(linked_responses),
            "unlinked": len(unlinked_responses),
            "linked_outside_decision_window": len(outside_window_responses),
            "unlinked_by_instance": dict(
                sorted(Counter(item.instance_id for item in unlinked_responses).items())
            ),
        },
        "details": details,
    }


@router.get("/v1/command-registry", dependencies=[Depends(require_admin)])
async def command_registry(request: Request) -> dict:
    try:
        registry = load_command_registry(request.app.state.settings.command_registry_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"command registry unavailable: {type(exc).__name__}",
        ) from exc
    return registry.as_dict()


@router.get("/v1/command-registry/runtime", dependencies=[Depends(require_admin)])
async def runtime_command_registry(request: Request, session: Session) -> dict:
    rows = (
        await session.scalars(
            select(CommandRegistrySnapshot).order_by(
                CommandRegistrySnapshot.instance_id,
                desc(CommandRegistrySnapshot.received_at),
            )
        )
    ).all()
    latest: dict[str, CommandRegistrySnapshot] = {}
    for row in rows:
        latest.setdefault(row.instance_id, row)

    try:
        registry = load_command_registry(request.app.state.settings.command_registry_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"command registry unavailable: {type(exc).__name__}",
        ) from exc

    snapshots = []
    combined_aliases: set[str] = set()
    uncovered_candidates: list[dict] = []
    now = datetime.now(timezone.utc)
    for row in latest.values():
        aliases = runtime_plugin_aliases(row.plugins_json)
        combined_aliases.update(aliases)
        for candidate in row.candidates_json:
            uncovered = [
                trigger
                for trigger in candidate.get("triggers", [])
                if not runtime_candidate_trigger_reviewed(registry, candidate, trigger)
            ]
            if uncovered:
                uncovered_candidates.append(
                    {**candidate, "instance_id": row.instance_id, "uncovered_triggers": uncovered}
                )
        received_at = row.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - received_at).total_seconds()))
        snapshots.append(
            {
                "instance_id": row.instance_id,
                "snapshot_hash": row.snapshot_hash,
                "observed_at": row.observed_at,
                "received_at": row.received_at,
                "age_seconds": age_seconds,
                "status": (
                    "fresh"
                    if age_seconds <= request.app.state.settings.command_registry_snapshot_stale_seconds
                    else "stale"
                ),
                "plugins": row.plugins_json,
                "candidates": row.candidates_json,
            }
        )

    static_rules = []
    serialized_rules = {item["id"]: item for item in registry.as_dict()["rules"]}
    for rule in registry.rules:
        payload = dict(serialized_rules[rule.id])
        payload["runtime_loaded"] = source_plugin_loaded(rule.source_plugin, combined_aliases) if latest else None
        static_rules.append(payload)

    return {
        "registry_version": registry.version,
        "snapshots": snapshots,
        "static_rules": static_rules,
        "uncovered_candidates": uncovered_candidates,
        "summary": {
            "snapshot_instances": len(snapshots),
            "fresh_snapshot_instances": sum(1 for item in snapshots if item["status"] == "fresh"),
            "stale_snapshot_instances": sum(1 for item in snapshots if item["status"] == "stale"),
            "loaded_plugins": sum(len(item["plugins"]) for item in snapshots),
            "runtime_matchers": sum(
                int(plugin.get("matcher_count", 0))
                for item in snapshots
                for plugin in item["plugins"]
            ),
            "unclassified_matchers": sum(
                max(
                    0,
                    int(plugin.get("matcher_count", 0))
                    - int(plugin.get("classified_matcher_count", 0)),
                )
                for item in snapshots
                for plugin in item["plugins"]
            ),
            "runtime_candidates": sum(len(item["candidates"]) for item in snapshots),
            "incomplete_runtime_candidates": sum(
                1
                for item in snapshots
                for candidate in item["candidates"]
                if candidate.get("complete") is not True
            ),
            "uncovered_candidate_triggers": sum(len(item["uncovered_triggers"]) for item in uncovered_candidates),
            "static_rules_loaded": sum(1 for item in static_rules if item["runtime_loaded"] is True),
            "static_rules_not_loaded": sum(1 for item in static_rules if item["runtime_loaded"] is False),
        },
    }


@router.get("/v1/events/{source_event_id}/context", dependencies=[Depends(require_admin)])
async def event_context(source_event_id: str, session: Session) -> dict:
    source = await session.get(SourceEvent, source_event_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source_event_id not found")

    observations = (
        await session.scalars(
            select(EventObservation)
            .where(EventObservation.source_event_id == source_event_id)
            .order_by(EventObservation.received_at)
        )
    ).all()
    links = (
        await session.scalars(
            select(EventLink).where(EventLink.from_source_event_id == source_event_id).order_by(EventLink.created_at)
        )
    ).all()
    decisions = (
        await session.scalars(
            select(EventDecision)
            .where(EventDecision.source_event_id == source_event_id)
            .order_by(EventDecision.created_at)
        )
    ).all()
    responses = (
        await session.scalars(
            select(ResponseRecord)
            .where(ResponseRecord.trigger_source_event_id == source_event_id)
            .order_by(ResponseRecord.received_at)
        )
    ).all()
    claims = (
        await session.scalars(
            select(EventClaim)
            .where(EventClaim.source_event_id == source_event_id)
            .order_by(EventClaim.created_at)
        )
    ).all()

    return {
        "source_event": {
            "source_event_id": source.id,
            "platform": source.platform,
            "event_type": source.event_type,
            "conversation_id": source.conversation_id,
            "conversation_type": source.conversation_type,
            "correlation_version": source.correlation_version,
            "occurred_at": source.occurred_at,
            "first_received_at": source.first_received_at,
        },
        "observations": [
            {
                "observation_id": item.id,
                "reported_source_event_id": item.reported_source_event_id,
                "instance_id": item.instance_id,
                "bot_id": item.bot_id,
                "platform_message_id": item.platform_message_id,
                "native_identity": item.metadata_json.get("native_identity"),
                "correlation_diagnostic": item.metadata_json.get("correlation"),
                "sender_id": item.sender_id,
                "sender_name": item.sender_name,
                "text": item.text,
                "received_at": item.received_at,
            }
            for item in observations
        ],
        "links": [
            {
                "link_id": item.id,
                "relation_type": item.relation_type,
                "to_source_event_id": item.to_source_event_id,
                "target_platform_message_id": item.target_platform_message_id,
                "resolver_status": item.resolver_status,
            }
            for item in links
        ],
        "decisions": [
            {
                "decision_id": item.id,
                "policy_version": item.policy_version,
                "decision_type": item.decision_type,
                "target_instance_id": item.target_instance_id,
                "confidence": item.confidence,
                "reason": item.reason,
                "features": item.features_json,
                "revision": item.revision,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in decisions
        ],
        "responses": [
            {
                "response_id": item.id,
                "source_response_id": item.source_response_id,
                "instance_id": item.instance_id,
                "response_type": item.response_type,
                "platform_message_id": item.platform_message_id,
                "text": item.text,
                "success": item.success,
                "received_at": item.received_at,
            }
            for item in responses
        ],
        "claims": [claim_record_payload(item) for item in claims],
    }


@router.get("/v1/instances", dependencies=[Depends(require_admin)])
async def instances(request: Request, session: Session) -> list[dict]:
    rows = (await session.scalars(select(BotInstance).order_by(BotInstance.id))).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "instance_id": item.id,
            "platform": item.platform,
            "adapter": item.adapter,
            "bot_id": item.bot_id,
            "role": item.role,
            "display_name": item.display_name,
            "version": item.version,
            "status": effective_status(item, request.app.state.settings.stale_after_seconds),
            "reported_status": item.reported_status,
            "heartbeat_age_seconds": (
                None
                if item.last_heartbeat_at is None
                else max(
                    0,
                    int(
                        (
                            now
                            - (
                                item.last_heartbeat_at
                                if item.last_heartbeat_at.tzinfo
                                else item.last_heartbeat_at.replace(tzinfo=timezone.utc)
                            )
                        ).total_seconds()
                    ),
                )
            ),
            "last_heartbeat_at": item.last_heartbeat_at,
            "last_event_at": item.last_event_at,
            "last_response_at": item.last_response_at,
            "capabilities": item.metadata_json.get("capabilities"),
        }
        for item in rows
    ]
