"""Durable exact-conversation Agent coordination and native text delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
from typing import Any

from fastapi import HTTPException, status
import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    AgentBudget,
    AgentDispatchIn,
    AgentRunCreateIn,
    AgentTextDeliveryCompleteIn,
    AgentTextDeliveryLeaseOut,
    AgentToolPromotionIn,
    EventIn,
    canonicalize_json_value,
)

from .agent_run_service import create_agent_run
from .agent_tool_loop_service import promote_wolfram_proposal, refresh_agent_tool_loop
from .auth import InvocationIdentity
from .database import Database
from .models import (
    AgentInteraction,
    AgentInteractionEvent,
    AgentModelProfileRecord,
    AgentRun,
    AgentRunAttempt,
    AgentTextDeliveryEvent,
    AgentTextDeliveryIntent,
    AgentToolContinuation,
    AgentToolLoop,
    AgentToolProposalRecord,
    EventObservation,
    SourceEvent,
    new_id,
)
from .settings import Settings
from .tool_invocation_service import database_now


logger = logging.getLogger(__name__)
_TERMINAL_INTERACTIONS = {"succeeded", "failed", "ambiguous", "expired"}
_GENERIC_FAILURE_TEXT = "这次处理没有可靠完成，请稍后再试。"


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _snapshot(value: Any) -> tuple[Any, str]:
    canonical = canonicalize_json_value(value)
    return canonical.value, canonical.sha256


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _lock_conversation_admission(
    session: AsyncSession,
    conversation_key: str,
) -> None:
    """Serialize exact-conversation quota checks through the accepting commit."""

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        lock_key = int.from_bytes(
            hashlib.sha256(conversation_key.encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    elif dialect == "sqlite":
        # Tests and local development use SQLite. An IMMEDIATE transaction
        # serializes the subsequent read/check/insert sequence across writers.
        await session.execute(text("BEGIN IMMEDIATE"))
    else:
        raise RuntimeError("Agent product admission requires SQLite or PostgreSQL")


async def accept_agent_interaction(
    session: AsyncSession,
    payload: EventIn,
    observation: EventObservation,
    *,
    authenticated_instance: str,
    settings: Settings,
) -> tuple[AgentInteraction | None, bool, str]:
    """Accept only addressed messages from reviewed instances/conversations."""

    if settings.agent_product_mode != "canary":
        return None, False, "product_mode_off"
    if authenticated_instance not in settings.agent_entry_instances:
        return None, False, "instance_not_allowed"
    if payload.instance.instance_id != authenticated_instance:
        return None, False, "instance_identity_mismatch"
    conversation_key = (
        f"{payload.instance.platform}:{payload.conversation.type}:{payload.conversation.id}"
    )
    if conversation_key not in settings.agent_canary_conversations:
        return None, False, "conversation_not_allowed"
    if payload.event_type != "message" or payload.message is None or payload.sender is None:
        return None, False, "not_a_user_message"
    if payload.conversation.type not in {"group", "private"}:
        return None, False, "conversation_type_not_supported"
    if observation.metadata_json.get("is_tome") is not True:
        return None, False, "not_explicitly_addressed"
    trigger_kind = str(observation.metadata_json.get("agent_trigger_kind") or "")
    if trigger_kind not in {"mention", "reply", "explicit"}:
        return None, False, "trigger_kind_unproven"

    await _lock_conversation_admission(session, conversation_key)
    existing = await session.scalar(
        select(AgentInteraction).where(
            AgentInteraction.instance_id == authenticated_instance,
            AgentInteraction.source_event_id == observation.source_event_id,
        )
    )
    if existing is not None:
        return existing, True, "duplicate"
    now = await database_now(session)
    active_count = await session.scalar(
        select(func.count(AgentInteraction.id)).where(
            AgentInteraction.conversation_key == conversation_key,
            AgentInteraction.state.not_in(_TERMINAL_INTERACTIONS),
        )
    )
    if (active_count or 0) >= settings.agent_max_concurrent_per_conversation:
        return None, False, "conversation_busy"
    recent_count = await session.scalar(
        select(func.count(AgentInteraction.id)).where(
            AgentInteraction.conversation_key == conversation_key,
            AgentInteraction.created_at
            >= now - timedelta(seconds=settings.agent_rate_window_seconds),
        )
    )
    if (recent_count or 0) >= settings.agent_max_interactions_per_window:
        return None, False, "conversation_rate_limited"
    day_start = now.astimezone(timezone.utc).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    daily_count = await session.scalar(
        select(func.count(AgentInteraction.id)).where(
            AgentInteraction.conversation_key == conversation_key,
            AgentInteraction.created_at >= day_start,
        )
    )
    if (daily_count or 0) >= settings.agent_max_interactions_per_day:
        return None, False, "conversation_daily_budget_exhausted"
    interaction = AgentInteraction(
        id=new_id(),
        instance_id=authenticated_instance,
        source_event_id=observation.source_event_id,
        conversation_key=conversation_key,
        conversation_type=payload.conversation.type,
        conversation_id=payload.conversation.id,
        reply_to_platform_message_id=payload.message.id,
        trigger_kind=trigger_kind,
        state="accepted",
        resource_version=1,
        reason_code="exact_canary_accepted",
        deadline_at=now + timedelta(seconds=settings.agent_delivery_deadline_seconds),
        terminal_at=None,
        updated_at=now,
    )
    evidence, evidence_hash = _snapshot(
        {
            "instance_id": authenticated_instance,
            "source_event_id": observation.source_event_id,
            "conversation_key": conversation_key,
            "trigger_kind": trigger_kind,
            "model_provider_id": settings.agent_model_provider_id,
            "model_profile_version": settings.agent_model_profile_version,
            "tool_scope": ["wolfram.run@1.1.0"],
            "delivery_scope": "native_text_once",
        }
    )
    session.add(interaction)
    # The append-only evidence row references this newly created authority row.
    # Flush explicitly so PostgreSQL cannot choose the event insert first when
    # no ORM relationship is mapped between the two model classes.
    await session.flush([interaction])
    session.add(
        AgentInteractionEvent(
            id=new_id(),
            interaction_id=interaction.id,
            sequence=1,
            event="accept",
            previous_state=None,
            state="accepted",
            reason_code="exact_canary_accepted",
            evidence_json=evidence,
            evidence_hash=evidence_hash,
        )
    )
    await session.commit()
    await session.refresh(interaction)
    return interaction, False, "accepted"


async def _transition(
    session: AsyncSession,
    interaction: AgentInteraction,
    *,
    state: str,
    event: str,
    reason: str,
    evidence: dict[str, Any],
    run_id: str | None = None,
    loop_id: str | None = None,
) -> None:
    previous = interaction.state
    next_version = interaction.resource_version + 1
    evidence_json, evidence_hash = _snapshot(evidence)
    event_record = AgentInteractionEvent(
        id=new_id(),
        interaction_id=interaction.id,
        sequence=next_version,
        event=event,
        previous_state=previous,
        state=state,
        reason_code=reason,
        evidence_json=evidence_json,
        evidence_hash=evidence_hash,
    )
    session.add(event_record)
    await session.flush([event_record])
    interaction.state = state
    interaction.resource_version = next_version
    interaction.reason_code = reason
    if run_id is not None:
        interaction.run_id = run_id
    if loop_id is not None:
        interaction.loop_id = loop_id
    now = await database_now(session)
    interaction.updated_at = now
    interaction.terminal_at = now if state in _TERMINAL_INTERACTIONS else None
    await session.commit()
    await session.refresh(interaction)


def _product_budget() -> AgentBudget:
    return AgentBudget(
        max_model_attempts=2,
        max_model_turns=2,
        max_tool_proposals=1,
        max_tool_calls=1,
        max_sequential_depth=1,
        max_parallel_fanout=1,
        max_wall_time_ms=60_000,
        max_input_tokens=32_000,
        max_output_tokens=2_048,
        max_total_tokens=34_048,
        max_cost_microunits=100_000,
        max_input_bytes=131_072,
        max_output_bytes=32_768,
        max_result_bytes=32_768,
        max_artifact_bytes=0,
    )


async def _create_product_run(
    session: AsyncSession,
    interaction: AgentInteraction,
    settings: Settings,
) -> None:
    profile = await session.scalar(
        select(AgentModelProfileRecord).where(
            AgentModelProfileRecord.provider_id == settings.agent_model_provider_id,
            AgentModelProfileRecord.version == settings.agent_model_profile_version,
        )
    )
    if profile is None:
        await _transition(
            session,
            interaction,
            state="failed",
            event="fail",
            reason="model_profile_not_found",
            evidence={},
        )
        return
    run, _ = await create_agent_run(
        session,
        AgentRunCreateIn(
            source_event_id=interaction.source_event_id,
            model_provider_id=profile.provider_id,
            model_profile_version=profile.version,
            model_profile_hash=profile.profile_hash,
            routing_reason="exact_test_group_fast_path",
            budget=_product_budget(),
        ),
        InvocationIdentity(caller="system", subject="agent-product-coordinator-v1"),
        f"agent-product:{interaction.id}",
        settings,
    )
    await session.refresh(interaction)
    await _transition(
        session,
        interaction,
        state="planning",
        event="run_create",
        reason="planner_context_ready",
        evidence={"run_id": run.id, "context_hash": run.context_hash},
        run_id=run.id,
    )


async def _dispatch(
    settings: Settings,
    *,
    target_type: str,
    target_id: str,
) -> None:
    payload = AgentDispatchIn(target_type=target_type, target_id=target_id)
    async with httpx.AsyncClient(
        base_url=settings.agent_provider_trigger_url,
        timeout=65,
        trust_env=False,
    ) as client:
        response = await client.post(
            "/v1/attempts",
            json=payload.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {settings.agent_provider_trigger_token}"},
        )
        if response.status_code not in {200, 201, 202, 409}:
            response.raise_for_status()


async def _latest_answer_for_run(
    session: AsyncSession,
    run_id: str,
) -> str | None:
    attempt = await session.scalar(
        select(AgentRunAttempt)
        .where(AgentRunAttempt.run_id == run_id, AgentRunAttempt.outcome == "succeeded")
        .order_by(AgentRunAttempt.attempt_number.desc())
    )
    if attempt is None or not isinstance(attempt.proposal_json, dict):
        return None
    answer = attempt.proposal_json.get("answer_markdown")
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


async def _latest_continuation_answer(
    session: AsyncSession,
    loop_id: str,
) -> str | None:
    continuation = await session.scalar(
        select(AgentToolContinuation)
        .where(AgentToolContinuation.loop_id == loop_id)
        .order_by(AgentToolContinuation.attempt_number.desc())
    )
    proposal = (
        continuation.report_json.get("proposal")
        if continuation is not None and isinstance(continuation.report_json, dict)
        else None
    )
    answer = proposal.get("answer_markdown") if isinstance(proposal, dict) else None
    return answer.strip() if isinstance(answer, str) and answer.strip() else None


async def _create_delivery(
    session: AsyncSession,
    interaction: AgentInteraction,
    settings: Settings,
    text: str,
    *,
    reason: str,
) -> None:
    bounded = text.strip()[:8_192]
    if not bounded:
        bounded = _GENERIC_FAILURE_TEXT
    existing = await session.scalar(
        select(AgentTextDeliveryIntent).where(
            AgentTextDeliveryIntent.interaction_id == interaction.id
        )
    )
    if existing is None:
        now = await database_now(session)
        content_hash = hashlib.sha256(bounded.encode("utf-8")).hexdigest()
        existing = AgentTextDeliveryIntent(
            id=new_id(),
            interaction_id=interaction.id,
            instance_id=interaction.instance_id,
            conversation_key=interaction.conversation_key,
            conversation_type=interaction.conversation_type,
            conversation_id=interaction.conversation_id,
            reply_to_platform_message_id=interaction.reply_to_platform_message_id,
            content_text=bounded,
            content_sha256=content_hash,
            state="pending",
            fence=0,
            deadline_at=min(
                _aware(interaction.deadline_at),
                now + timedelta(seconds=settings.agent_delivery_deadline_seconds),
            ),
            terminal_at=None,
            updated_at=now,
        )
        evidence, evidence_hash = _snapshot(
            {
                "interaction_id": interaction.id,
                "content_sha256": content_hash,
                "content_bytes": len(bounded.encode("utf-8")),
                "conversation_key": interaction.conversation_key,
                "reply_to_platform_message_id": interaction.reply_to_platform_message_id,
            }
        )
        session.add(existing)
        await session.flush([existing])
        session.add(
            AgentTextDeliveryEvent(
                id=new_id(),
                intent_id=existing.id,
                sequence=1,
                event="create",
                previous_state=None,
                state="pending",
                reason_code=reason,
                evidence_json=evidence,
                evidence_hash=evidence_hash,
            )
        )
        await session.commit()
        await session.refresh(interaction)
    if interaction.state != "delivery_pending":
        await _transition(
            session,
            interaction,
            state="delivery_pending",
            event="delivery_create",
            reason=reason,
            evidence={"intent_id": existing.id, "content_sha256": existing.content_sha256},
        )


async def _advance_one(
    session: AsyncSession,
    interaction: AgentInteraction,
    settings: Settings,
) -> None:
    now = await database_now(session)
    if now >= _aware(interaction.deadline_at) and interaction.state != "delivery_pending":
        await _transition(
            session,
            interaction,
            state="expired",
            event="expire",
            reason="interaction_deadline",
            evidence={},
        )
        return
    if interaction.state == "accepted":
        await _create_product_run(session, interaction, settings)
        return
    if interaction.state == "planning":
        run = await session.get(AgentRun, interaction.run_id)
        if run is None:
            raise RuntimeError("interaction references a missing AgentRun")
        if run.state == "context_ready":
            await _dispatch(settings, target_type="run", target_id=run.id)
            return
        if run.state == "shadow_complete":
            proposal = await session.scalar(
                select(AgentToolProposalRecord)
                .where(
                    AgentToolProposalRecord.run_id == run.id,
                    AgentToolProposalRecord.validation == "valid",
                )
                .order_by(AgentToolProposalRecord.ordinal)
            )
            if proposal is not None:
                loop, _ = await promote_wolfram_proposal(
                    session,
                    run.id,
                    AgentToolPromotionIn(proposal_id=proposal.id),
                    settings,
                )
                await session.refresh(interaction)
                if loop.state == "tool_pending":
                    await _transition(
                        session,
                        interaction,
                        state="tool_pending",
                        event="tool_promote",
                        reason="wolfram_invocation_queued",
                        evidence={"loop_id": loop.id, "invocation_id": loop.invocation_id},
                        loop_id=loop.id,
                    )
                else:
                    await _create_delivery(
                        session, interaction, settings, _GENERIC_FAILURE_TEXT,
                        reason="tool_promotion_failed",
                    )
                return
            answer = await _latest_answer_for_run(session, run.id)
            await _create_delivery(
                session,
                interaction,
                settings,
                answer or _GENERIC_FAILURE_TEXT,
                reason="direct_answer_ready" if answer else "direct_answer_missing",
            )
            return
        if run.state in {"failed", "timed_out", "budget_exhausted", "cancelled", "rejected"}:
            await _create_delivery(
                session, interaction, settings, _GENERIC_FAILURE_TEXT,
                reason=f"planner_{run.state}",
            )
        return
    if interaction.state in {"tool_pending", "continuing"}:
        loop = await session.get(AgentToolLoop, interaction.loop_id)
        if loop is None:
            raise RuntimeError("interaction references a missing AgentToolLoop")
        loop = await refresh_agent_tool_loop(session, loop)
        if loop.state == "result_ready":
            if interaction.state != "continuing":
                await _transition(
                    session,
                    interaction,
                    state="continuing",
                    event="continuation_start",
                    reason="untrusted_tool_result_ready",
                    evidence={"loop_id": loop.id, "result_hash": loop.result_hash},
                )
            await _dispatch(settings, target_type="tool_loop", target_id=loop.id)
            return
        if loop.state == "complete":
            answer = await _latest_continuation_answer(session, loop.id)
            await _create_delivery(
                session,
                interaction,
                settings,
                answer or _GENERIC_FAILURE_TEXT,
                reason="tool_answer_ready" if answer else "tool_answer_missing",
            )
            return
        if loop.state in {"failed", "budget_exhausted"}:
            await _create_delivery(
                session, interaction, settings, _GENERIC_FAILURE_TEXT,
                reason=f"tool_loop_{loop.state}",
            )


async def advance_agent_product(database: Database, settings: Settings) -> None:
    if settings.agent_product_mode != "canary":
        return
    async with database.sessions() as session:
        interactions = list(
            (
                await session.scalars(
                    select(AgentInteraction)
                    .where(AgentInteraction.state.not_in(_TERMINAL_INTERACTIONS | {"delivery_pending"}))
                    .order_by(AgentInteraction.created_at)
                    .limit(20)
                )
            ).all()
        )
        for interaction in interactions:
            interaction_id = interaction.id
            try:
                await _advance_one(session, interaction, settings)
            except Exception:
                await session.rollback()
                logger.exception(
                    "Agent product interaction advance failed: %s",
                    interaction_id,
                )


async def reap_agent_deliveries(session: AsyncSession) -> None:
    now = await database_now(session)
    intents = list(
        (
            await session.scalars(
                select(AgentTextDeliveryIntent).where(
                    AgentTextDeliveryIntent.state.in_(("pending", "leased"))
                )
            )
        ).all()
    )
    for intent in intents:
        if intent.state == "leased" and intent.lease_expires_at is not None and now >= _aware(intent.lease_expires_at):
            await _finish_delivery(
                session,
                intent,
                outcome="ambiguous",
                platform_message_id=None,
                safe_error_code="lease_expired_completion_unknown",
                reason="lease_expire",
            )
        elif intent.state == "pending" and now >= _aware(intent.deadline_at):
            await _finish_delivery(
                session,
                intent,
                outcome="expired",
                platform_message_id=None,
                safe_error_code="delivery_deadline",
                reason="deadline",
            )


async def lease_agent_delivery(
    session: AsyncSession,
    *,
    instance_id: str,
    settings: Settings,
) -> AgentTextDeliveryLeaseOut | None:
    if settings.agent_product_mode != "canary" or instance_id not in settings.agent_entry_instances:
        return None
    await reap_agent_deliveries(session)
    intent = await session.scalar(
        select(AgentTextDeliveryIntent)
        .where(
            AgentTextDeliveryIntent.instance_id == instance_id,
            AgentTextDeliveryIntent.state == "pending",
        )
        .order_by(AgentTextDeliveryIntent.created_at)
        .with_for_update(skip_locked=True)
    )
    if intent is None:
        return None
    now = await database_now(session)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    lease_expires_at = now + timedelta(seconds=settings.agent_delivery_lease_seconds)
    evidence, evidence_hash = _snapshot(
        {"instance_id": instance_id, "fence": 1, "lease_token_hash": token_hash}
    )
    event_record = AgentTextDeliveryEvent(
        id=new_id(),
        intent_id=intent.id,
        sequence=2,
        event="lease",
        previous_state="pending",
        state="leased",
        reason_code="adapter_lease",
        evidence_json=evidence,
        evidence_hash=evidence_hash,
    )
    session.add(event_record)
    await session.flush([event_record])
    intent.state = "leased"
    intent.fence = 1
    intent.lease_token_hash = token_hash
    intent.lease_expires_at = lease_expires_at
    intent.updated_at = now
    await session.commit()
    return AgentTextDeliveryLeaseOut(
        intent_id=intent.id,
        interaction_id=intent.interaction_id,
        instance_id=intent.instance_id,
        conversation_key=intent.conversation_key,
        conversation_type=intent.conversation_type,
        conversation_id=intent.conversation_id,
        reply_to_platform_message_id=intent.reply_to_platform_message_id,
        text=intent.content_text,
        content_sha256=intent.content_sha256,
        fence=intent.fence,
        lease_token=token,
        lease_expires_at=_aware(intent.lease_expires_at).isoformat(),
    )


async def _finish_delivery(
    session: AsyncSession,
    intent: AgentTextDeliveryIntent,
    *,
    outcome: str,
    platform_message_id: str | None,
    safe_error_code: str | None,
    reason: str,
) -> None:
    previous = intent.state
    now = await database_now(session)
    state = outcome
    evidence, evidence_hash = _snapshot(
        {
            "outcome": outcome,
            "platform_message_id": platform_message_id,
            "safe_error_code": safe_error_code,
            "fence": intent.fence,
        }
    )
    event_record = AgentTextDeliveryEvent(
        id=new_id(),
        intent_id=intent.id,
        sequence=3 if previous == "leased" else 2,
        event=reason,
        previous_state=previous,
        state=state,
        reason_code=safe_error_code or "platform_send_succeeded",
        evidence_json=evidence,
        evidence_hash=evidence_hash,
    )
    session.add(event_record)
    await session.flush([event_record])
    intent.state = state
    intent.platform_message_id = platform_message_id
    intent.safe_error_code = safe_error_code
    intent.terminal_at = now
    intent.updated_at = now
    interaction = await session.get(AgentInteraction, intent.interaction_id)
    if interaction is None:
        raise RuntimeError("delivery intent references a missing interaction")
    terminal_state = "succeeded" if state == "succeeded" else "ambiguous" if state == "ambiguous" else "expired" if state == "expired" else "failed"
    await session.flush()
    await _transition(
        session,
        interaction,
        state=terminal_state,
        event="delivery_complete",
        reason=safe_error_code or "platform_send_succeeded",
        evidence={"intent_id": intent.id, "outcome": state, "platform_message_id": platform_message_id},
    )


async def complete_agent_delivery(
    session: AsyncSession,
    intent_id: str,
    payload: AgentTextDeliveryCompleteIn,
    *,
    authenticated_instance: str,
) -> AgentTextDeliveryIntent:
    if payload.instance_id != authenticated_instance:
        raise _conflict("delivery identity does not match bearer token")
    intent = await session.get(AgentTextDeliveryIntent, intent_id)
    if intent is None or intent.instance_id != authenticated_instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delivery intent not found")
    if intent.state in {"succeeded", "failed", "ambiguous"}:
        if (
            intent.state == payload.outcome
            and intent.platform_message_id == payload.platform_message_id
            and intent.safe_error_code == payload.safe_error_code
        ):
            return intent
        raise _conflict("delivery intent already has a different terminal receipt")
    if intent.state != "leased" or payload.fence != intent.fence:
        raise _conflict("delivery lease is no longer active")
    supplied_hash = hashlib.sha256(payload.lease_token.encode("utf-8")).hexdigest()
    if intent.lease_token_hash is None or not secrets.compare_digest(
        supplied_hash, intent.lease_token_hash
    ):
        raise _conflict("delivery lease token is invalid")
    if payload.outcome == "succeeded":
        if not payload.platform_message_id or payload.safe_error_code is not None:
            raise _conflict("successful delivery requires a platform message ID")
    elif not payload.safe_error_code or payload.platform_message_id is not None:
        raise _conflict("unsuccessful delivery requires a safe error and no message ID")
    await _finish_delivery(
        session,
        intent,
        outcome=payload.outcome,
        platform_message_id=payload.platform_message_id,
        safe_error_code=payload.safe_error_code,
        reason="complete",
    )
    await session.refresh(intent)
    return intent


def interaction_view(interaction: AgentInteraction, *, duplicate: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "accepted": True,
        "duplicate": duplicate,
        "interaction_id": interaction.id,
        "source_event_id": interaction.source_event_id,
        "conversation_key": interaction.conversation_key,
        "state": interaction.state,
        "reason_code": interaction.reason_code,
        "deadline_at": interaction.deadline_at,
    }
