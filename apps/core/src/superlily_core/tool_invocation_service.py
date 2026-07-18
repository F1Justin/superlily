"""第三阶段只记账调用服务；本模块不包含 lease 或 Provider 执行入口。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    TERMINAL_INVOCATION_STATES,
    InvocationState,
    InvocationTransitionEvent,
    ToolDescriptor,
    ToolInvocationCreateIn,
    ToolRegistryContractError,
    canonicalize_json_value,
    invocation_request_hash,
    validate_invocation_transition,
    validate_schema_instance,
)

from .auth import InvocationIdentity
from .models import (
    ToolDescriptorRecord,
    ToolInvocation,
    ToolInvocationTransition,
    new_id,
)
from .settings import Settings, ToolRolloutScope
from .tool_registry_service import tool_registry_view


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.current_timestamp()))
    if not isinstance(value, datetime):
        raise RuntimeError("database did not return an authoritative timestamp")
    return _aware(value)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool invocation not found")


def _snapshot(value: Any) -> tuple[Any, str]:
    canonical = canonicalize_json_value(value)
    return canonical.value, canonical.sha256


def canonical_invocation_conversation(payload: ToolInvocationCreateIn) -> str:
    return f"{payload.principal.platform}:{payload.principal.conversation_id}"


def _matching_rollout_scope(
    settings: Settings,
    payload: ToolInvocationCreateIn,
    identity: InvocationIdentity,
) -> ToolRolloutScope | None:
    if settings.tool_execution_mode == "canary":
        scopes = settings.tool_canary_scopes
    elif settings.tool_execution_mode == "enforce":
        scopes = settings.tool_enforce_scopes
    else:
        return None
    conversation = canonical_invocation_conversation(payload)
    return next(
        (
            scope
            for scope in scopes
            if scope.tool_id == payload.tool_id
            and scope.descriptor_version == payload.descriptor_version
            and scope.descriptor_hash == payload.descriptor_hash
            and scope.canonical_conversation == conversation
            and scope.caller == identity.caller
        ),
        None,
    )


async def _rate_limit_exceeded(
    session: AsyncSession,
    descriptor: ToolDescriptor,
    payload: ToolInvocationCreateIn,
    selected_scope: ToolRolloutScope,
    database_time: datetime,
) -> bool:
    rate = descriptor.rate_limit
    statement = select(ToolInvocation).where(
        ToolInvocation.created_at
        >= database_time - timedelta(seconds=rate.window_seconds),
        ToolInvocation.state.not_in({"rejected", "recorded_only"}),
        ToolInvocation.execution_mode.in_({"canary", "enforce"}),
    )
    if rate.scope in {"tool", "provider", "conversation", "sender"}:
        statement = statement.where(ToolInvocation.tool_id == descriptor.tool_id)
    rows = list((await session.scalars(statement.order_by(ToolInvocation.created_at))).all())
    canonical_conversation = canonical_invocation_conversation(payload)
    count = 0
    for row in rows:
        if rate.scope == "provider":
            matched = row.policy_snapshot_json.get("selected_provider_id") == selected_scope.provider_id
        elif rate.scope == "conversation":
            matched = (
                row.policy_snapshot_json.get("canonical_conversation")
                == canonical_conversation
            )
        elif rate.scope == "sender":
            facts = row.principal_snapshot_json.get("facts", {})
            matched = (
                facts.get("platform") == payload.principal.platform
                and facts.get("sender_id") == payload.principal.sender_id
            )
        else:
            matched = True
        if matched:
            count += 1
            if count >= rate.requests:
                return True
    return False


async def _descriptor_record(
    session: AsyncSession,
    payload: ToolInvocationCreateIn,
) -> ToolDescriptorRecord:
    record = await session.scalar(
        select(ToolDescriptorRecord).where(
            ToolDescriptorRecord.tool_id == payload.tool_id,
            ToolDescriptorRecord.version == payload.descriptor_version,
        )
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="exact tool descriptor version not found",
        )
    if record.descriptor_hash != payload.descriptor_hash:
        raise _conflict("requested descriptor hash does not match immutable authority")
    return record


async def _existing_invocation(
    session: AsyncSession,
    identity: InvocationIdentity,
    idempotency_key: str,
) -> ToolInvocation | None:
    return await session.scalar(
        select(ToolInvocation).where(
            ToolInvocation.creator_type == identity.caller,
            ToolInvocation.creator_id == identity.subject,
            ToolInvocation.idempotency_key == idempotency_key,
        )
    )


async def create_tool_invocation(
    session: AsyncSession,
    payload: ToolInvocationCreateIn,
    identity: InvocationIdentity,
    idempotency_key: str,
    settings: Settings,
) -> tuple[ToolInvocation, bool]:
    request_hash = invocation_request_hash(
        payload,
        caller=identity.caller,
        authenticated_subject=identity.subject,
    )
    existing = await _existing_invocation(session, identity, idempotency_key)
    if existing is not None:
        if existing.request_hash != request_hash:
            raise _conflict("invocation idempotency key was reused with different request content")
        return existing, True

    if settings.tool_execution_mode == "off":
        raise _conflict("tool invocation creation is disabled")
    descriptor_record = await _descriptor_record(session, payload)
    descriptor = ToolDescriptor.model_validate(descriptor_record.descriptor_json)
    try:
        validate_schema_instance(payload.input, descriptor.input_schema)
    except ToolRegistryContractError as exc:
        raise _unprocessable("tool input does not satisfy the reviewed descriptor") from exc

    registry = await tool_registry_view(session, settings, tool_id=payload.tool_id)
    registry_version = next(
        (
            item
            for item in registry["tools"]
            if item["version"] == payload.descriptor_version
            and item["desired"]["descriptor_hash"] == payload.descriptor_hash
        ),
        None,
    )
    if registry_version is None:
        raise _conflict("descriptor disappeared while evaluating invocation policy")

    input_value, input_hash = _snapshot(payload.input)
    hard_reasons: list[str] = []
    if identity.caller not in descriptor.allowed_callers:
        hard_reasons.append("caller_forbidden")
    if descriptor.permission != "public":
        hard_reasons.append("principal_unauthorized")
    missing_capabilities = sorted(set(descriptor.required_capabilities) - set(payload.capabilities))
    if missing_capabilities:
        hard_reasons.append("capability_unavailable")
    if descriptor_record.lifecycle in {"retired", "revoked"}:
        hard_reasons.append("inactive_descriptor")

    input_limit = descriptor.resource_budget.input_bytes
    if input_limit is not None and len(canonicalize_json_value(payload.input).canonical_bytes) > input_limit:
        hard_reasons.append("input_budget_exceeded")

    effective_reasons = list(registry_version["effective"]["reasons"])
    database_time = await database_now(session)
    selected_scope = _matching_rollout_scope(settings, payload, identity)
    selected_runtime: dict[str, Any] | None = None
    execution_reasons: list[str] = []
    if settings.tool_execution_mode in {"canary", "enforce"}:
        if selected_scope is None:
            execution_reasons.append("rollout_scope_mismatch")
        else:
            selected_runtime = next(
                (
                    item
                    for item in registry_version["reported"]
                    if item["provider_id"] == selected_scope.provider_id
                    and item["runtime_eligible"]
                ),
                None,
            )
            if selected_runtime is None:
                execution_reasons.extend(effective_reasons or ["provider_ineligible"])
        if descriptor.confirmation != "never":
            execution_reasons.append("confirmation_unavailable")
        if (
            selected_scope is not None
            and selected_runtime is not None
            and await _rate_limit_exceeded(
                session,
                descriptor,
                payload,
                selected_scope,
                database_time,
            )
        ):
            execution_reasons.append("rate_limited")
        execution_reasons.extend(effective_reasons)

    reasons = list(dict.fromkeys([*effective_reasons, *hard_reasons, *execution_reasons]))
    if hard_reasons or execution_reasons:
        decision = "rejected"
        reason_code = (hard_reasons or execution_reasons)[0]
        event: InvocationTransitionEvent = "reject"
    elif settings.tool_execution_mode == "ledger_only":
        decision = "recorded_only"
        reason_code = "ledger_only"
        event = "record_only"
    else:
        decision = "queued"
        reason_code = f"{settings.tool_execution_mode}_queued"
        event = "queue"
    descriptor_snapshot, _ = _snapshot(descriptor.model_dump(mode="json"))
    principal_snapshot, principal_hash = _snapshot(
        {
            "authenticated_caller": identity.caller,
            "authenticated_subject": identity.subject,
            "facts": payload.principal.model_dump(mode="json"),
        }
    )
    capabilities, capability_hash = _snapshot(sorted(payload.capabilities))
    policy_snapshot, policy_hash = _snapshot(
        {
            "schema_version": "1.0",
            "evaluated_at": database_time.isoformat(),
            "execution_mode": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
            "decision": decision,
            "eligible_if_execution_enabled": not reasons,
            "queue_created": decision == "queued",
            "lease_created": False,
            "effective_reasons": reasons,
            "missing_capabilities": missing_capabilities,
            "descriptor_lifecycle": descriptor_record.lifecycle,
            "review_status": descriptor_record.review_status,
            "side_effect": descriptor.side_effect,
            "determinism": descriptor.determinism,
            "retry_policy": descriptor.retry_policy,
            "permission": descriptor.permission,
            "confirmation": descriptor.confirmation,
            "allowed_callers": descriptor.allowed_callers,
            "canonical_conversation": canonical_invocation_conversation(payload),
            "selected_provider_id": (
                None if selected_scope is None else selected_scope.provider_id
            ),
            "rollout_scope": (
                None
                if selected_scope is None
                else {
                    "tool_id": selected_scope.tool_id,
                    "descriptor_version": selected_scope.descriptor_version,
                    "descriptor_hash": selected_scope.descriptor_hash,
                    "canonical_conversation": selected_scope.canonical_conversation,
                    "caller": selected_scope.caller,
                    "provider_id": selected_scope.provider_id,
                }
            ),
            "rate_limit": descriptor.rate_limit.model_dump(mode="json"),
            "selected_inventory_hash": (
                None if selected_runtime is None else selected_runtime["inventory_hash"]
            ),
            "selected_implementation_hash": (
                None if selected_runtime is None else selected_runtime["implementation_hash"]
            ),
            "resource_budget": descriptor.resource_budget.model_dump(mode="json"),
            "execution_permissions": descriptor.execution_permissions.model_dump(mode="json"),
            "provider_observations": registry_version["reported"],
        }
    )
    invocation = ToolInvocation(
        id=new_id(),
        creator_type=identity.caller,
        creator_id=identity.subject,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        descriptor_id=descriptor_record.id,
        tool_id=descriptor_record.tool_id,
        descriptor_version=descriptor_record.version,
        descriptor_hash=descriptor_record.descriptor_hash,
        descriptor_snapshot_json=descriptor_snapshot,
        input_json=input_value,
        input_hash=input_hash,
        principal_snapshot_json=principal_snapshot,
        principal_hash=principal_hash,
        capability_snapshot_json=capabilities,
        capability_hash=capability_hash,
        policy_snapshot_json=policy_snapshot,
        policy_hash=policy_hash,
        selected_provider_id=(None if selected_scope is None else selected_scope.provider_id),
        execution_mode=settings.tool_execution_mode,
        state=decision,
        transition_sequence=2,
        reason_code=reason_code,
        deadline_at=database_time + timedelta(milliseconds=descriptor.timeout_ms),
        terminal_at=(database_time if decision in TERMINAL_INVOCATION_STATES else None),
        created_at=database_time,
        updated_at=database_time,
    )
    session.add(invocation)
    proposed_evidence, proposed_evidence_hash = _snapshot(
        {
            "request_hash": request_hash,
            "descriptor_hash": descriptor_record.descriptor_hash,
            "input_hash": input_hash,
            "principal_hash": principal_hash,
            "capability_hash": capability_hash,
            "execution_mode": settings.tool_execution_mode,
        }
    )
    decision_evidence, decision_evidence_hash = _snapshot(
        {
            "policy_hash": policy_hash,
            "decision": decision,
            "reason_code": reason_code,
            "effective_reasons": reasons,
            "eligible_if_execution_enabled": policy_snapshot[
                "eligible_if_execution_enabled"
            ],
            "queue_created": decision == "queued",
            "lease_created": False,
        }
    )
    session.add_all(
        [
            ToolInvocationTransition(
                invocation_id=invocation.id,
                sequence=1,
                event="propose",
                previous_state=None,
                state="proposed",
                actor_type=identity.caller,
                actor_id=identity.subject,
                reason_code="proposal_received",
                evidence_json=proposed_evidence,
                evidence_hash=proposed_evidence_hash,
                created_at=database_time,
            ),
            ToolInvocationTransition(
                invocation_id=invocation.id,
                sequence=2,
                event=event,
                previous_state="proposed",
                state=decision,
                actor_type="system",
                actor_id="tool-policy-v1",
                reason_code=reason_code,
                evidence_json=decision_evidence,
                evidence_hash=decision_evidence_hash,
                created_at=database_time,
            ),
        ]
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await _existing_invocation(session, identity, idempotency_key)
        if duplicate is not None and duplicate.request_hash == request_hash:
            return duplicate, True
        if duplicate is not None:
            raise _conflict(
                "invocation idempotency key was reused with different request content"
            ) from exc
        raise _conflict("concurrent invocation creation conflict") from exc
    await session.refresh(invocation)
    return invocation, False


async def invocation_transitions(
    session: AsyncSession,
    invocation_id: str,
) -> list[ToolInvocationTransition]:
    return list(
        (
            await session.scalars(
                select(ToolInvocationTransition)
                .where(ToolInvocationTransition.invocation_id == invocation_id)
                .order_by(ToolInvocationTransition.sequence)
            )
        ).all()
    )


async def invocation_view(session: AsyncSession, invocation: ToolInvocation) -> dict[str, Any]:
    transitions = await invocation_transitions(session, invocation.id)
    return {
        "schema_version": "1.0",
        "invocation_id": invocation.id,
        "tool": {
            "tool_id": invocation.tool_id,
            "descriptor_version": invocation.descriptor_version,
            "descriptor_hash": invocation.descriptor_hash,
        },
        "creator": {
            "type": invocation.creator_type,
            "id": invocation.creator_id,
        },
        "request": {
            "request_hash": invocation.request_hash,
            "input": invocation.input_json,
            "input_hash": invocation.input_hash,
            "principal": invocation.principal_snapshot_json,
            "principal_hash": invocation.principal_hash,
            "capabilities": invocation.capability_snapshot_json,
            "capability_hash": invocation.capability_hash,
        },
        "policy": invocation.policy_snapshot_json,
        "policy_hash": invocation.policy_hash,
        "selected_provider_id": invocation.selected_provider_id,
        "execution_mode": invocation.execution_mode,
        "state": invocation.state,
        "reason_code": invocation.reason_code,
        "deadline_at": invocation.deadline_at,
        "terminal_at": invocation.terminal_at,
        "created_at": invocation.created_at,
        "updated_at": invocation.updated_at,
        "transitions": [
            {
                "sequence": transition.sequence,
                "event": transition.event,
                "previous_state": transition.previous_state,
                "state": transition.state,
                "actor_type": transition.actor_type,
                "actor_id": transition.actor_id,
                "reason_code": transition.reason_code,
                "evidence": transition.evidence_json,
                "evidence_hash": transition.evidence_hash,
                "created_at": transition.created_at,
            }
            for transition in transitions
        ],
    }


def _authorize_invocation(invocation: ToolInvocation, identity: InvocationIdentity) -> None:
    if identity.caller == "admin_api":
        return
    if invocation.creator_type != identity.caller or invocation.creator_id != identity.subject:
        raise _not_found()


async def get_tool_invocation(
    session: AsyncSession,
    invocation_id: str,
    identity: InvocationIdentity,
) -> ToolInvocation:
    invocation = await session.get(ToolInvocation, invocation_id)
    if invocation is None:
        raise _not_found()
    _authorize_invocation(invocation, identity)
    return invocation


async def append_invocation_transition(
    session: AsyncSession,
    invocation_id: str,
    *,
    event: InvocationTransitionEvent,
    state: InvocationState,
    actor_type: str,
    actor_id: str,
    reason_code: str,
    evidence: dict[str, Any],
    commit: bool = True,
) -> ToolInvocation:
    invocation = await session.scalar(
        select(ToolInvocation).where(ToolInvocation.id == invocation_id).with_for_update()
    )
    if invocation is None:
        raise _not_found()
    previous_state: InvocationState = invocation.state  # type: ignore[assignment]
    try:
        validate_invocation_transition(previous_state, state, event)
    except ValueError as exc:
        raise _conflict(str(exc)) from exc
    database_time = await database_now(session)
    next_sequence = invocation.transition_sequence + 1
    terminal_at = database_time if state in TERMINAL_INVOCATION_STATES else None
    result = await session.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.id == invocation.id,
            ToolInvocation.state == previous_state,
            ToolInvocation.transition_sequence == invocation.transition_sequence,
        )
        .values(
            state=state,
            transition_sequence=next_sequence,
            reason_code=reason_code,
            terminal_at=terminal_at,
            updated_at=database_time,
        )
    )
    if result.rowcount != 1:
        raise _conflict("invocation state changed concurrently")
    evidence_value, evidence_hash = _snapshot(evidence)
    session.add(
        ToolInvocationTransition(
            invocation_id=invocation.id,
            sequence=next_sequence,
            event=event,
            previous_state=previous_state,
            state=state,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            evidence_json=evidence_value,
            evidence_hash=evidence_hash,
            created_at=database_time,
        )
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
    refreshed = await session.get(ToolInvocation, invocation.id, populate_existing=True)
    assert refreshed is not None
    return refreshed


async def cancel_tool_invocation(
    session: AsyncSession,
    invocation_id: str,
    identity: InvocationIdentity,
    reason: str,
) -> ToolInvocation:
    invocation = await get_tool_invocation(session, invocation_id, identity)
    if invocation.state in TERMINAL_INVOCATION_STATES:
        raise _conflict("terminal invocation is immutable")
    if invocation.state in {"leased", "running"}:
        event: InvocationTransitionEvent = "request_cancel"
        next_state: InvocationState = "cancel_requested"
    elif invocation.state in {"proposed", "awaiting_confirmation", "queued"}:
        event = "cancel"
        next_state = "cancelled"
    else:
        raise _conflict("invocation cannot be cancelled from its current state")
    return await append_invocation_transition(
        session,
        invocation.id,
        event=event,
        state=next_state,
        actor_type=identity.caller,
        actor_id=identity.subject,
        reason_code="caller_cancelled",
        evidence={"reason": reason},
    )


async def reap_expired_invocations(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[str]:
    now = await database_now(session)
    statement = (
        select(ToolInvocation)
        .where(
            ToolInvocation.state.not_in(TERMINAL_INVOCATION_STATES),
            ToolInvocation.deadline_at <= now,
        )
        .order_by(ToolInvocation.deadline_at, ToolInvocation.created_at)
        .limit(max(1, min(limit, 1_000)))
        .with_for_update(skip_locked=True)
    )
    expired = list((await session.scalars(statement)).all())
    transitioned: list[str] = []
    for invocation in expired:
        if invocation.state == "awaiting_confirmation":
            event: InvocationTransitionEvent = "confirmation_expire"
            state: InvocationState = "expired"
        elif invocation.state == "queued":
            event = "timeout"
            state = "timed_out"
        elif invocation.state == "proposed":
            event = "reject"
            state = "rejected"
        else:
            event = "unknown_completion"
            state = "unknown_completion"
        await append_invocation_transition(
            session,
            invocation.id,
            event=event,
            state=state,
            actor_type="reaper",
            actor_id="tool-invocation-reaper-v1",
            reason_code="deadline_expired",
            evidence={"database_time": now.isoformat()},
            commit=False,
        )
        transitioned.append(invocation.id)
    await session.commit()
    return transitioned
