from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn, HeartbeatIn, ResponseIn

from .auth import ingest_identity, require_admin
from .command_registry import load_command_registry
from .dependencies import get_session
from .models import BotInstance, EventDecision, EventLink, EventObservation, ResponseRecord, SourceEvent
from .service import effective_status, ingest_event, ingest_heartbeat, ingest_response

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


@router.get("/v1/decisions/recent", dependencies=[Depends(require_admin)])
async def recent_decisions(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventDecision).order_by(desc(EventDecision.created_at)).limit(limit))).all()
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
            "created_at": item.created_at,
        }
        for item in rows
    ]


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
                "created_at": item.created_at,
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
        }
        for item in rows
    ]
