from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn, SanitizationPolicy, sanitize_payload

from .correlation import canonical_conversation_id
from .models import EventObservation, PlatformAPICallRecord, utc_now
from .settings import Settings


def _trigger_source_event_id(payload: EventIn) -> str | None:
    for reference in payload.references:
        if reference.type == "derived_from" and reference.source_event_id:
            return reference.source_event_id
    return None


async def record_platform_api_call(
    session: AsyncSession,
    payload: EventIn,
    observation: EventObservation,
    settings: Settings,
) -> None:
    audit = payload.platform_api_call
    if audit is None:
        return
    conversation_id = canonical_conversation_id(
        payload.instance.platform,
        payload.conversation.type,
        payload.conversation.id,
    )
    safe_parameters = sanitize_payload(
        audit.safe_parameters,
        SanitizationPolicy(
            enabled=True,
            max_bytes=min(settings.raw_max_bytes, 16_384),
            max_string=2_048,
        ),
    ) or {}
    trigger_source_event_id = _trigger_source_event_id(payload)
    record = await session.scalar(
        select(PlatformAPICallRecord).where(
            PlatformAPICallRecord.instance_id == payload.instance.instance_id,
            PlatformAPICallRecord.call_id == audit.call_id,
        )
    )
    if record is None:
        record = PlatformAPICallRecord(
            instance_id=payload.instance.instance_id,
            call_id=audit.call_id,
            api_name=audit.api_name,
            target_conversation_id=conversation_id,
            target_conversation_type=payload.conversation.type,
            trigger_reported_source_event_id=trigger_source_event_id,
            safe_parameters_json=safe_parameters,
            start_observed=False,
            result_observed=False,
            outcome="pending",
            success=None,
            result_message_ids_json=[],
        )
        session.add(record)
    elif any(
        (
            record.api_name != audit.api_name,
            record.target_conversation_id != conversation_id,
            record.target_conversation_type != payload.conversation.type,
            record.trigger_reported_source_event_id != trigger_source_event_id,
            record.safe_parameters_json != safe_parameters,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="platform API call identity was reused for different call details",
        )

    if audit.stage == "started":
        if record.start_observed and record.started_observation_id != observation.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="platform API call already has a different start event",
            )
        record.start_observed = True
        record.started_observation_id = observation.id
        record.started_at = payload.occurred_at
    else:
        terminal = (
            audit.outcome,
            audit.success,
            audit.return_code,
            audit.duration_ms,
            audit.result_message_ids,
            audit.safe_error_code,
        )
        existing_terminal = (
            record.outcome,
            record.success,
            record.return_code,
            record.duration_ms,
            record.result_message_ids_json,
            record.safe_error_code,
        )
        if record.result_observed and existing_terminal != terminal:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="platform API call already has a different completion result",
            )
        record.result_observed = True
        record.completed_observation_id = observation.id
        record.outcome = audit.outcome
        record.success = audit.success
        record.return_code = audit.return_code
        record.duration_ms = audit.duration_ms
        record.result_message_ids_json = audit.result_message_ids
        record.safe_error_code = audit.safe_error_code
        record.completed_at = payload.occurred_at
    record.updated_at = utc_now()
