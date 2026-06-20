from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn, HeartbeatIn, ResponseIn

from .auth import ingest_identity, require_admin
from .dependencies import get_session
from .models import BotInstance, EventObservation, ResponseRecord, SourceEvent
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
