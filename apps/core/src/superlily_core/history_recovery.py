from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn
from superlily_contracts.history_recovery import HistoryRecoveryProgress

from .models import EventObservation, QQHistoryRecoveryProgress


def validate_recovery_progress(payload: EventIn) -> HistoryRecoveryProgress | None:
    if payload.event_type != "audit.history_recovery":
        return None
    try:
        return HistoryRecoveryProgress.model_validate(payload.metadata.get("history_recovery"))
    except ValidationError as exc:
        raise HTTPException(422, "invalid history recovery progress") from exc


def record_recovery_progress(
    session: AsyncSession,
    payload: EventIn,
    observation: EventObservation,
    progress: HistoryRecoveryProgress | None,
) -> None:
    if progress is None:
        return
    session.add(QQHistoryRecoveryProgress(
        observation_id=observation.id,
        instance_id=payload.instance.instance_id,
        conversation_type=payload.conversation.type,
        conversation_id=payload.conversation.id,
        observed_at=payload.occurred_at,
        **progress.model_dump(),
    ))
