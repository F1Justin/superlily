"""Phase 5b one-tool Wolfram loop with untrusted result reinjection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from superlily_contracts import (
    AgentAttemptReportIn,
    AgentToolPromotionIn,
    ModelProviderProfile,
    ToolInvocationCreateIn,
    canonicalize_json_value,
)

from .agent_run_service import _expected_cost, _model_route
from .auth import InvocationIdentity
from .models import (
    AgentRun,
    AgentRunAttempt,
    AgentToolContinuation,
    AgentToolLoop,
    AgentToolLoopEvent,
    AgentToolProposalRecord,
    ToolAttempt,
    ToolInvocation,
    new_id,
)
from .settings import Settings
from .tool_invocation_service import create_tool_invocation, database_now


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="agent tool loop not found")


def _snapshot(value: Any) -> tuple[Any, str, int]:
    canonical = canonicalize_json_value(value)
    return canonical.value, canonical.sha256, len(canonical.canonical_bytes)


async def _continuation_route_profile(
    session: AsyncSession,
    run: AgentRun,
    loop: AgentToolLoop,
) -> tuple[ModelProviderProfile, str]:
    route = _model_route(run)
    initial_attempts = list(
        (
            await session.scalars(
                select(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run.id)
                .order_by(AgentRunAttempt.attempt_number)
            )
        ).all()
    )
    successful = [item for item in initial_attempts if item.outcome == "succeeded"]
    if not successful:
        raise RuntimeError("agent tool loop has no successful planner attempt")
    current_provider = successful[-1].provider_id
    continuations = list(
        (
            await session.scalars(
                select(AgentToolContinuation)
                .where(AgentToolContinuation.loop_id == loop.id)
                .order_by(AgentToolContinuation.attempt_number)
            )
        ).all()
    )
    if continuations:
        last = continuations[-1]
        current_provider = last.provider_id
        outcome = str(last.report_json["outcome"])
        if outcome in {"provider_error", "invalid_output", "timed_out"}:
            for index, (profile, _) in enumerate(route):
                if profile.provider_id == current_provider and index + 1 < len(route):
                    return route[index + 1]
    for profile, profile_hash in route:
        if profile.provider_id == current_provider:
            return profile, profile_hash
    raise RuntimeError("continuation provider is outside the frozen model route")


async def _event(
    session: AsyncSession,
    loop: AgentToolLoop,
    *,
    event: str,
    state: str,
    reason_code: str,
    evidence: dict[str, Any],
    terminal_at: datetime | None = None,
    result_json: Any | None = None,
    result_hash: str | None = None,
    result_bytes: int = 0,
) -> None:
    evidence_json, evidence_hash, _ = _snapshot(evidence)
    previous = loop.state
    next_version = loop.resource_version + 1
    event_record = AgentToolLoopEvent(
        id=new_id(),
        loop_id=loop.id,
        sequence=next_version,
        event=event,
        previous_state=previous,
        state=state,
        reason_code=reason_code,
        evidence_json=evidence_json,
        evidence_hash=evidence_hash,
    )
    session.add(event_record)
    await session.flush([event_record])
    loop.resource_version = next_version
    loop.state = state
    loop.reason_code = reason_code
    loop.terminal_at = terminal_at
    loop.result_json = result_json
    loop.result_hash = result_hash
    loop.result_bytes = result_bytes
    loop.updated_at = await database_now(session)


async def promote_wolfram_proposal(
    session: AsyncSession,
    run_id: str,
    payload: AgentToolPromotionIn,
    settings: Settings,
) -> tuple[AgentToolLoop, bool]:
    if settings.agent_mode != "bounded_readonly":
        raise _conflict("bounded agent execution is disabled")
    existing = await session.scalar(select(AgentToolLoop).where(AgentToolLoop.run_id == run_id))
    if existing is not None:
        if existing.proposal_id != payload.proposal_id:
            raise _conflict("AgentRun already promoted a different proposal")
        return existing, True
    run = await session.get(AgentRun, run_id)
    proposal = await session.get(AgentToolProposalRecord, payload.proposal_id)
    if (
        run is None
        or run.state != "shadow_complete"
        or proposal is None
        or proposal.run_id != run.id
        or proposal.validation != "valid"
    ):
        raise _not_found()
    budget = run.budget_snapshot_json
    if (
        int(budget["max_tool_calls"]) != 1
        or int(budget["max_sequential_depth"]) != 1
        or int(budget["max_parallel_fanout"]) != 1
        or int(budget["max_result_bytes"]) <= 0
        or int(budget["max_artifact_bytes"]) != 0
    ):
        raise _conflict("AgentRun does not carry the exact one-call execution budget")
    if (
        proposal.tool_id != "wolfram.run"
        or proposal.descriptor_version != "1.1.0"
    ):
        raise _conflict("initial Phase 5b only promotes wolfram.run@1.1.0")

    principal = run.principal_snapshot_json
    platform = principal["platform"]
    prefix = f"{platform}:"
    if not run.conversation_key.startswith(prefix):
        raise _conflict("AgentRun conversation identity is inconsistent")
    invocation_payload = ToolInvocationCreateIn(
        tool_id=proposal.tool_id,
        descriptor_version=proposal.descriptor_version,
        descriptor_hash=proposal.descriptor_hash,
        input=proposal.arguments_json,
        principal={
            "platform": platform,
            "sender_id": principal["sender_id"],
            "conversation_id": run.conversation_key[len(prefix) :],
            "conversation_type": principal["conversation_type"],
            "platform_roles": principal["observed_platform_roles"],
            "source_event_id": run.source_event_id,
        },
        capabilities=[],
    )
    invocation, _ = await create_tool_invocation(
        session,
        invocation_payload,
        InvocationIdentity(caller="agent", subject=run.id),
        f"agent-tool:{run.id}:{proposal.proposal_hash}",
        settings,
    )
    now = await database_now(session)
    executable = invocation.state == "queued"
    loop = AgentToolLoop(
        id=new_id(),
        run_id=run.id,
        proposal_id=proposal.id,
        invocation_id=invocation.id,
        state="tool_pending" if executable else "failed",
        resource_version=1,
        reason_code="tool_invocation_queued" if executable else invocation.reason_code,
        result_json=None,
        result_hash=None,
        result_bytes=0,
        terminal_at=None if executable else now,
        updated_at=now,
    )
    evidence_json, evidence_hash, _ = _snapshot(
        {
            "run_id": run.id,
            "proposal_id": proposal.id,
            "proposal_hash": proposal.proposal_hash,
            "invocation_id": invocation.id,
            "invocation_state": invocation.state,
            "tool_id": proposal.tool_id,
            "descriptor_version": proposal.descriptor_version,
            "descriptor_hash": proposal.descriptor_hash,
            "max_tool_calls": 1,
            "delivery_authority": False,
        }
    )
    session.add(loop)
    await session.flush([loop])
    session.add(
        AgentToolLoopEvent(
            id=new_id(),
            loop_id=loop.id,
            sequence=1,
            event="promote",
            previous_state=None,
            state=loop.state,
            reason_code=loop.reason_code,
            evidence_json=evidence_json,
            evidence_hash=evidence_hash,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        concurrent = await session.scalar(
            select(AgentToolLoop).where(AgentToolLoop.run_id == run.id)
        )
        if concurrent is not None and concurrent.proposal_id == proposal.id:
            return concurrent, True
        raise _conflict("concurrent AgentRun promotion conflict") from exc
    await session.refresh(loop)
    return loop, False


async def refresh_agent_tool_loop(
    session: AsyncSession,
    loop: AgentToolLoop,
) -> AgentToolLoop:
    if loop.state != "tool_pending":
        return loop
    invocation = await session.get(ToolInvocation, loop.invocation_id)
    if invocation is None:
        raise RuntimeError("agent tool loop references a missing invocation")
    if invocation.state == "succeeded":
        attempt = await session.scalar(
            select(ToolAttempt)
            .where(
                ToolAttempt.invocation_id == invocation.id,
                ToolAttempt.state == "succeeded",
            )
            .order_by(ToolAttempt.attempt_number.desc())
        )
        if attempt is None or attempt.output_json is None:
            return loop
        result = {
            "boundary": "BEGIN_UNTRUSTED_TOOL_RESULT / END_UNTRUSTED_TOOL_RESULT",
            "untrusted": True,
            "data_classification": "conversation",
            "source": {
                "invocation_id": invocation.id,
                "tool_id": invocation.tool_id,
                "descriptor_version": invocation.descriptor_version,
                "descriptor_hash": invocation.descriptor_hash,
                "provider_id": attempt.provider_id,
                "output_hash": attempt.output_hash,
            },
            "output": attempt.output_json,
        }
        result_json, result_hash, result_bytes = _snapshot(result)
        run = await session.get(AgentRun, loop.run_id)
        if run is None:
            raise RuntimeError("agent tool loop references a missing run")
        max_result_bytes = int(run.budget_snapshot_json["max_result_bytes"])
        if result_bytes > max_result_bytes:
            now = await database_now(session)
            await _event(
                session,
                loop,
                event="budget_exhaust",
                state="budget_exhausted",
                reason_code="max_result_bytes",
                evidence={
                    "result_hash": result_hash,
                    "result_bytes": result_bytes,
                    "max_result_bytes": max_result_bytes,
                },
                terminal_at=now,
            )
        else:
            await _event(
                session,
                loop,
                event="tool_result",
                state="result_ready",
                reason_code="untrusted_result_captured",
                evidence={
                    "result_hash": result_hash,
                    "result_bytes": result_bytes,
                    "invocation_id": invocation.id,
                },
                result_json=result_json,
                result_hash=result_hash,
                result_bytes=result_bytes,
            )
        await session.commit()
        await session.refresh(loop)
    elif invocation.terminal_at is not None:
        now = await database_now(session)
        await _event(
            session,
            loop,
            event="tool_fail",
            state="failed",
            reason_code=f"tool_{invocation.state}",
            evidence={
                "invocation_id": invocation.id,
                "invocation_state": invocation.state,
                "invocation_reason": invocation.reason_code,
            },
            terminal_at=now,
        )
        await session.commit()
        await session.refresh(loop)
    return loop


async def continuation_input(
    session: AsyncSession,
    loop_id: str,
    provider_id: str,
    settings: Settings,
) -> dict[str, Any]:
    if settings.agent_mode != "bounded_readonly":
        raise _not_found()
    loop = await session.get(AgentToolLoop, loop_id)
    if loop is None:
        raise _not_found()
    loop = await refresh_agent_tool_loop(session, loop)
    run = await session.get(AgentRun, loop.run_id)
    if run is None or loop.state != "result_ready":
        raise _not_found()
    profile, profile_hash = await _continuation_route_profile(session, run, loop)
    if profile.provider_id != provider_id:
        raise _not_found()
    return {
        "schema_version": "1.0",
        "run_id": run.id,
        "loop_id": loop.id,
        "context_hash": run.context_hash,
        "context": run.context_snapshot_json,
        "tool_results": [loop.result_json],
        "budget": run.budget_snapshot_json,
        "budget_hash": run.budget_hash,
        "model_profile": profile.model_dump(mode="json"),
        "model_profile_hash": profile_hash,
        "routing_reason": run.routing_reason,
        "deadline_at": run.deadline_at,
        "tool_execution_authority": False,
        "delivery_authority": False,
    }


async def record_continuation(
    session: AsyncSession,
    loop_id: str,
    payload: AgentAttemptReportIn,
    *,
    provider_id: str,
    idempotency_key: str,
    settings: Settings,
) -> tuple[AgentToolContinuation, AgentToolLoop, bool]:
    if settings.agent_mode != "bounded_readonly":
        raise _conflict("bounded agent execution is disabled")
    report_json, report_hash, _ = _snapshot(payload.model_dump(mode="json"))
    existing = await session.scalar(
        select(AgentToolContinuation).where(
            AgentToolContinuation.provider_id == provider_id,
            AgentToolContinuation.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.loop_id != loop_id or existing.report_hash != report_hash:
            raise _conflict("continuation idempotency key was reused")
        loop = await session.get(AgentToolLoop, loop_id)
        if loop is None:
            raise RuntimeError("continuation references a missing loop")
        return existing, loop, True
    loop = await session.get(AgentToolLoop, loop_id)
    if loop is None:
        raise _not_found()
    loop = await refresh_agent_tool_loop(session, loop)
    run = await session.get(AgentRun, loop.run_id)
    if run is None or loop.state != "result_ready":
        raise _not_found()
    profile, active_profile_hash = await _continuation_route_profile(
        session,
        run,
        loop,
    )
    if profile.provider_id != provider_id:
        raise _not_found()
    if payload.proposal is not None and payload.proposal.tool_proposals:
        raise _conflict("one-call Phase 5b loop rejects further tool proposals")
    if payload.usage.cost_microunits != _expected_cost(profile, payload.usage):
        raise _conflict("continuation cost does not match reviewed pricing")
    prior_attempts = list(
        (
            await session.scalars(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == run.id)
            )
        ).all()
    )
    prior_continuations = list(
        (
            await session.scalars(
                select(AgentToolContinuation)
                .where(AgentToolContinuation.loop_id == loop.id)
                .order_by(AgentToolContinuation.attempt_number)
            )
        ).all()
    )
    next_attempt = len(prior_attempts) + len(prior_continuations) + 1
    totals = {
        key: sum(int(item.usage_json[key]) for item in prior_attempts)
        + sum(
            int(item.report_json["usage"][key])
            for item in prior_continuations
        )
        + int(getattr(payload.usage, key))
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_microunits",
            "input_bytes",
            "output_bytes",
            "wall_time_ms",
        )
    }
    budget = run.budget_snapshot_json
    exhausted = [
        budget_key
        for usage_key, budget_key in (
            ("input_tokens", "max_input_tokens"),
            ("output_tokens", "max_output_tokens"),
            ("total_tokens", "max_total_tokens"),
            ("cost_microunits", "max_cost_microunits"),
            ("input_bytes", "max_input_bytes"),
            ("output_bytes", "max_output_bytes"),
            ("wall_time_ms", "max_wall_time_ms"),
        )
        if totals[usage_key] > int(budget[budget_key])
    ]
    if 2 > int(budget["max_model_turns"]):
        exhausted.append("max_model_turns")
    if next_attempt > int(budget["max_model_attempts"]):
        exhausted.append("max_model_attempts")
    if await database_now(session) > run.deadline_at.replace(
        tzinfo=run.deadline_at.tzinfo or timezone.utc
    ):
        exhausted.append("deadline")
    continuation = AgentToolContinuation(
        id=new_id(),
        loop_id=loop.id,
        attempt_number=next_attempt,
        provider_id=provider_id,
        idempotency_key=idempotency_key,
        report_json=report_json,
        report_hash=report_hash,
    )
    session.add(continuation)
    now = await database_now(session)
    if exhausted:
        state = "budget_exhausted"
        reason = "continuation_budget_exhausted"
        event = "budget_exhaust"
    elif payload.outcome == "succeeded":
        state = "complete"
        reason = "bounded_loop_complete_no_delivery"
        event = "complete"
    elif (
        payload.outcome in {"provider_error", "invalid_output", "timed_out"}
        and next_attempt < int(budget["max_model_attempts"])
    ):
        state = "result_ready"
        reason = payload.safe_error_code or "continuation_retry_available"
        event = "model_retry"
    else:
        state = "failed"
        reason = payload.safe_error_code or "continuation_failed"
        event = "model_fail"
    await _event(
        session,
        loop,
        event=event,
        state=state,
        reason_code=reason,
        evidence={
            "continuation_id": continuation.id,
            "report_hash": report_hash,
            "outcome": payload.outcome,
            "model_profile_hash": active_profile_hash,
            "routing_reason": run.routing_reason,
            "total_usage": totals,
            "budget_reasons": exhausted,
            "additional_tool_invocations": 0,
            "delivery_intents_created": 0,
        },
        terminal_at=None if state == "result_ready" else now,
        result_json=loop.result_json,
        result_hash=loop.result_hash,
        result_bytes=loop.result_bytes,
    )
    await session.commit()
    await session.refresh(continuation)
    await session.refresh(loop)
    return continuation, loop, False


async def agent_tool_loop_view(
    session: AsyncSession,
    loop: AgentToolLoop,
) -> dict[str, Any]:
    loop = await refresh_agent_tool_loop(session, loop)
    events = list(
        (
            await session.scalars(
                select(AgentToolLoopEvent)
                .where(AgentToolLoopEvent.loop_id == loop.id)
                .order_by(AgentToolLoopEvent.sequence)
            )
        ).all()
    )
    continuations = list(
        (
            await session.scalars(
                select(AgentToolContinuation)
                .where(AgentToolContinuation.loop_id == loop.id)
                .order_by(AgentToolContinuation.attempt_number)
            )
        ).all()
    )
    continuation = continuations[-1] if continuations else None
    return {
        "schema_version": "1.0",
        "loop_id": loop.id,
        "run_id": loop.run_id,
        "proposal_id": loop.proposal_id,
        "invocation_id": loop.invocation_id,
        "state": loop.state,
        "resource_version": loop.resource_version,
        "reason_code": loop.reason_code,
        "result_hash": loop.result_hash,
        "result_bytes": loop.result_bytes,
        "continuation_report_hash": (
            None if continuation is None else continuation.report_hash
        ),
        "continuation_attempt_count": len(continuations),
        "tool_invocation_count": 1,
        "delivery_intent_count": 0,
        "terminal_at": loop.terminal_at,
        "events": [
            {
                "sequence": item.sequence,
                "event": item.event,
                "previous_state": item.previous_state,
                "state": item.state,
                "reason_code": item.reason_code,
                "evidence": item.evidence_json,
                "evidence_hash": item.evidence_hash,
                "created_at": item.created_at,
            }
            for item in events
        ],
    }
