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
    ToolInvocationConfirmIn,
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
    ToolConfirmation,
    ToolConfirmationEvent,
    ToolInvocation,
    ToolInvocationTransition,
    ToolProvider,
    ToolRolloutPlanItemRecord,
    ToolRolloutPlanRecord,
    new_id,
)
from .rollout_service import (
    consume_rollout_invocation,
    locked_rollout_for_lease,
    matching_active_rollout,
)
from .settings import Settings
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


def descriptor_requires_confirmation(descriptor: ToolDescriptor) -> bool:
    return descriptor.confirmation == "always" or (
        descriptor.confirmation == "on_write"
        and descriptor.side_effect in {"write", "admin", "external_message"}
    )


async def _rate_limit_exceeded(
    session: AsyncSession,
    descriptor: ToolDescriptor,
    payload: ToolInvocationCreateIn,
    selected_scope: ToolRolloutPlanItemRecord,
    database_time: datetime,
    exclude_invocation_id: str | None = None,
) -> bool:
    rate = descriptor.rate_limit
    statement = select(ToolInvocation).where(
        ToolInvocation.created_at
        >= database_time - timedelta(seconds=rate.window_seconds),
        ToolInvocation.state.not_in({"rejected", "recorded_only"}),
        ToolInvocation.execution_mode.in_({"canary", "enforce"}),
    )
    if exclude_invocation_id is not None:
        statement = statement.where(ToolInvocation.id != exclude_invocation_id)
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
    confirmation_required = descriptor_requires_confirmation(descriptor)
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
    selected_rollout: tuple[ToolRolloutPlanRecord, ToolRolloutPlanItemRecord] | None = None
    if settings.tool_execution_mode == "canary":
        selected_rollout = await matching_active_rollout(
            session,
            database_time=database_time,
            tool_id=payload.tool_id,
            descriptor_version=payload.descriptor_version,
            descriptor_hash=payload.descriptor_hash,
            canonical_conversation=canonical_invocation_conversation(payload),
            caller=identity.caller,
        )
    selected_plan = None if selected_rollout is None else selected_rollout[0]
    selected_scope = None if selected_rollout is None else selected_rollout[1]
    selected_runtime: dict[str, Any] | None = None
    execution_reasons: list[str] = []
    rollout_fallback_reasons: list[str] = []
    effective_execution_mode = settings.tool_execution_mode
    if settings.tool_execution_mode == "canary":
        if selected_scope is None:
            rollout_fallback_reasons.append("reviewed_rollout_plan_unavailable")
        else:
            provider_record = await session.get(ToolProvider, selected_scope.provider_id)
            if descriptor_record.resource_version != selected_scope.expected_descriptor_resource_version:
                rollout_fallback_reasons.append("descriptor_resource_version_mismatch")
            if (
                provider_record is None
                or provider_record.lifecycle != "active"
                or provider_record.resource_version
                != selected_scope.expected_provider_resource_version
            ):
                rollout_fallback_reasons.append("provider_resource_version_mismatch")
        if settings.tool_global_stop:
            rollout_fallback_reasons.append("global_stop")
        if not rollout_fallback_reasons and selected_scope is not None:
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
            if descriptor.confirmation == "two_person":
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
        if not rollout_fallback_reasons:
            execution_reasons.extend(effective_reasons)

    if (
        settings.tool_execution_mode == "canary"
        and selected_plan is not None
        and selected_scope is not None
        and not hard_reasons
        and not execution_reasons
        and not rollout_fallback_reasons
        and not confirmation_required
        and not await consume_rollout_invocation(
            session,
            selected_plan,
            database_time=database_time,
        )
    ):
        rollout_fallback_reasons.append("rollout_invocation_limit_exhausted")

    if rollout_fallback_reasons:
        effective_execution_mode = "ledger_only"

    reasons = list(
        dict.fromkeys(
            [
                *effective_reasons,
                *hard_reasons,
                *execution_reasons,
                *rollout_fallback_reasons,
            ]
        )
    )
    if hard_reasons or execution_reasons:
        decision = "rejected"
        reason_code = (hard_reasons or execution_reasons)[0]
        event: InvocationTransitionEvent = "reject"
    elif effective_execution_mode == "ledger_only":
        decision = "recorded_only"
        reason_code = (
            "ledger_only"
            if not rollout_fallback_reasons
            else "rollout_fallback_ledger_only"
        )
        event = "record_only"
    elif confirmation_required:
        decision = "awaiting_confirmation"
        reason_code = "confirmation_required"
        event = "require_confirmation"
    else:
        decision = "queued"
        reason_code = f"{effective_execution_mode}_queued"
        event = "queue"
    confirmation_id = new_id() if decision == "awaiting_confirmation" else None
    confirmation_expires_at = (
        min(
            database_time + timedelta(seconds=settings.tool_confirmation_seconds),
            _aware(selected_plan.expires_at),
        )
        if confirmation_id is not None and selected_plan is not None
        else None
    )
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
            "schema_version": "1.1",
            "evaluated_at": database_time.isoformat(),
            "execution_mode": effective_execution_mode,
            "execution_ceiling": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
            "decision": decision,
            "eligible_if_execution_enabled": not reasons,
            "queue_created": decision == "queued",
            "lease_created": False,
            "confirmation_challenge": (
                None
                if confirmation_id is None
                else {
                    "confirmation_id": confirmation_id,
                    "required_approvals": 1,
                    "expires_at": confirmation_expires_at.isoformat(),
                }
            ),
            "effective_reasons": reasons,
            "rollout_fallback_reasons": rollout_fallback_reasons,
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
            "rollout_plan": (
                None
                if selected_plan is None
                else {
                    "record_id": selected_plan.id,
                    "plan_id": selected_plan.plan_id,
                    "version": selected_plan.version,
                    "plan_hash": selected_plan.plan_hash,
                    "resource_version": selected_plan.resource_version,
                    "starts_at": _aware(selected_plan.starts_at).isoformat(),
                    "expires_at": _aware(selected_plan.expires_at).isoformat(),
                    "max_invocations": selected_plan.max_invocations,
                    "rollback_mode": selected_plan.rollback_mode,
                }
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
                    "item_id": selected_scope.item_id,
                    "expected_descriptor_resource_version": (
                        selected_scope.expected_descriptor_resource_version
                    ),
                    "expected_provider_resource_version": (
                        selected_scope.expected_provider_resource_version
                    ),
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
        rollout_plan_id=(None if selected_plan is None else selected_plan.id),
        rollout_plan_item_id=(None if selected_scope is None else selected_scope.id),
        selected_provider_id=(None if selected_scope is None else selected_scope.provider_id),
        execution_mode=effective_execution_mode,
        state=decision,
        transition_sequence=2,
        reason_code=reason_code,
        deadline_at=(
            confirmation_expires_at
            if confirmation_expires_at is not None
            else database_time + timedelta(milliseconds=descriptor.timeout_ms)
        ),
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
            "execution_mode": effective_execution_mode,
            "execution_ceiling": settings.tool_execution_mode,
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
            "confirmation_id": confirmation_id,
        }
    )
    ledger_rows: list[Any] = [
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
    if confirmation_id is not None:
        assert confirmation_expires_at is not None
        confirmation_evidence, confirmation_evidence_hash = _snapshot(
            {
                "invocation_id": invocation.id,
                "request_hash": request_hash,
                "input_hash": input_hash,
                "principal_hash": principal_hash,
                "policy_hash": policy_hash,
                "caller_type": identity.caller,
                "caller_id": identity.subject,
                "expires_at": confirmation_expires_at.isoformat(),
            }
        )
        ledger_rows.extend(
            [
                ToolConfirmation(
                    id=confirmation_id,
                    invocation_id=invocation.id,
                    policy=descriptor.confirmation,
                    state="pending",
                    resource_version=1,
                    request_hash=request_hash,
                    input_hash=input_hash,
                    principal_hash=principal_hash,
                    policy_hash=policy_hash,
                    caller_type=identity.caller,
                    caller_id=identity.subject,
                    required_approvals=1,
                    expires_at=confirmation_expires_at,
                    created_at=database_time,
                    updated_at=database_time,
                ),
                ToolConfirmationEvent(
                    confirmation_id=confirmation_id,
                    sequence=1,
                    event="create",
                    previous_state=None,
                    state="pending",
                    actor_type="system",
                    actor_id="tool-confirmation-policy-v1",
                    idempotency_key=f"create:{confirmation_id}",
                    request_hash=request_hash,
                    reason="精确调用确认挑战已创建",
                    evidence_json=confirmation_evidence,
                    evidence_hash=confirmation_evidence_hash,
                    effective_at=database_time,
                    created_at=database_time,
                ),
            ]
        )
    session.add_all(ledger_rows)
    try:
        # ORM mapper 之间没有可写 relationship；先物化父 invocation，避免
        # PostgreSQL 在一次 flush 中先尝试插入 confirmation 子行。
        await session.flush([invocation])
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


async def confirmation_view(
    session: AsyncSession,
    invocation_id: str,
) -> dict[str, Any] | None:
    confirmation = await session.scalar(
        select(ToolConfirmation).where(ToolConfirmation.invocation_id == invocation_id)
    )
    if confirmation is None:
        return None
    events = list(
        (
            await session.scalars(
                select(ToolConfirmationEvent)
                .where(ToolConfirmationEvent.confirmation_id == confirmation.id)
                .order_by(ToolConfirmationEvent.sequence)
            )
        ).all()
    )
    return {
        "confirmation_id": confirmation.id,
        "policy": confirmation.policy,
        "state": confirmation.state,
        "resource_version": confirmation.resource_version,
        "request_hash": confirmation.request_hash,
        "input_hash": confirmation.input_hash,
        "principal_hash": confirmation.principal_hash,
        "policy_hash": confirmation.policy_hash,
        "caller": {
            "type": confirmation.caller_type,
            "id": confirmation.caller_id,
        },
        "required_approvals": confirmation.required_approvals,
        "expires_at": confirmation.expires_at,
        "consumed_at": confirmation.consumed_at,
        "rejected_at": confirmation.rejected_at,
        "expired_at": confirmation.expired_at,
        "created_at": confirmation.created_at,
        "updated_at": confirmation.updated_at,
        "events": [
            {
                "sequence": item.sequence,
                "event": item.event,
                "previous_state": item.previous_state,
                "state": item.state,
                "actor_type": item.actor_type,
                "actor_id": item.actor_id,
                "request_hash": item.request_hash,
                "reason": item.reason,
                "evidence": item.evidence_json,
                "evidence_hash": item.evidence_hash,
                "effective_at": item.effective_at,
                "created_at": item.created_at,
            }
            for item in events
        ],
    }


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
        "confirmation": await confirmation_view(session, invocation.id),
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
    deadline_at: datetime | None = None,
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
    update_values: dict[str, Any] = {
        "state": state,
        "transition_sequence": next_sequence,
        "reason_code": reason_code,
        "terminal_at": terminal_at,
        "updated_at": database_time,
    }
    if deadline_at is not None:
        update_values["deadline_at"] = deadline_at
    result = await session.execute(
        update(ToolInvocation)
        .where(
            ToolInvocation.id == invocation.id,
            ToolInvocation.state == previous_state,
            ToolInvocation.transition_sequence == invocation.transition_sequence,
        )
        .values(**update_values)
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


def _confirmation_decision_hash(
    invocation_id: str,
    payload: ToolInvocationConfirmIn,
    identity: InvocationIdentity,
) -> str:
    return canonicalize_json_value(
        {
            "schema_version": "1.0",
            "invocation_id": invocation_id,
            "caller": identity.caller,
            "authenticated_subject": identity.subject,
            "confirmation": payload.model_dump(mode="json"),
        }
    ).sha256


async def _transition_confirmation(
    session: AsyncSession,
    confirmation: ToolConfirmation,
    *,
    event: str,
    state: str,
    actor_type: str,
    actor_id: str,
    idempotency_key: str,
    decision_request_hash: str,
    reason: str,
    evidence: dict[str, Any],
    effective_at: datetime,
) -> ToolConfirmation:
    if confirmation.state != "pending":
        raise _conflict("confirmation is no longer pending")
    next_version = confirmation.resource_version + 1
    evidence_value, evidence_hash = _snapshot(evidence)
    session.add(
        ToolConfirmationEvent(
            confirmation_id=confirmation.id,
            sequence=next_version,
            event=event,
            previous_state="pending",
            state=state,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_hash=decision_request_hash,
            reason=reason,
            evidence_json=evidence_value,
            evidence_hash=evidence_hash,
            effective_at=effective_at,
            created_at=effective_at,
        )
    )
    await session.flush()
    values: dict[str, Any] = {
        "state": state,
        "resource_version": next_version,
        "updated_at": effective_at,
    }
    if state == "consumed":
        values["consumed_at"] = effective_at
    elif state == "rejected":
        values["rejected_at"] = effective_at
    elif state == "expired":
        values["expired_at"] = effective_at
    result = await session.execute(
        update(ToolConfirmation)
        .where(
            ToolConfirmation.id == confirmation.id,
            ToolConfirmation.state == "pending",
            ToolConfirmation.resource_version == confirmation.resource_version,
        )
        .values(**values)
    )
    if result.rowcount != 1:
        raise _conflict("confirmation state changed concurrently")
    refreshed = await session.get(ToolConfirmation, confirmation.id, populate_existing=True)
    assert refreshed is not None
    return refreshed


async def _reject_confirmation_approval(
    session: AsyncSession,
    invocation: ToolInvocation,
    confirmation: ToolConfirmation,
    identity: InvocationIdentity,
    *,
    idempotency_key: str,
    decision_request_hash: str,
    reason_code: str,
    database_time: datetime,
    expired: bool = False,
) -> None:
    await _transition_confirmation(
        session,
        confirmation,
        event="expire" if expired else "reject",
        state="expired" if expired else "rejected",
        actor_type=identity.caller,
        actor_id=identity.subject,
        idempotency_key=idempotency_key,
        decision_request_hash=decision_request_hash,
        reason=("确认挑战已经过期" if expired else "确认时执行权限已失效"),
        evidence={
            "requested_decision": "approve",
            "reason_code": reason_code,
            "database_time": database_time.isoformat(),
        },
        effective_at=database_time,
    )
    await append_invocation_transition(
        session,
        invocation.id,
        event="confirmation_expire" if expired else "reject",
        state="expired" if expired else "rejected",
        actor_type="system",
        actor_id="tool-confirmation-policy-v1",
        reason_code=reason_code,
        evidence={
            "confirmation_id": confirmation.id,
            "decision_request_hash": decision_request_hash,
            "database_time": database_time.isoformat(),
        },
        commit=False,
    )
    await session.commit()


async def _approval_revalidation_reason(
    session: AsyncSession,
    invocation: ToolInvocation,
    settings: Settings,
    database_time: datetime,
) -> tuple[
    str | None,
    ToolDescriptor | None,
    ToolRolloutPlanRecord | None,
    ToolRolloutPlanItemRecord | None,
]:
    if settings.tool_execution_mode != "canary":
        return "execution_mode_changed", None, None, None
    if settings.tool_global_stop:
        return "global_stop", None, None, None
    if (
        invocation.execution_mode != "canary"
        or invocation.rollout_plan_id is None
        or invocation.rollout_plan_item_id is None
        or invocation.selected_provider_id is None
    ):
        return "rollout_authority_missing", None, None, None
    rollout = await locked_rollout_for_lease(
        session,
        plan_record_id=invocation.rollout_plan_id,
        plan_item_id=invocation.rollout_plan_item_id,
        database_time=database_time,
    )
    if rollout is None:
        return "rollout_plan_inactive", None, None, None
    plan, scope = rollout
    plan_snapshot = invocation.policy_snapshot_json.get("rollout_plan")
    scope_snapshot = invocation.policy_snapshot_json.get("rollout_scope")
    if not isinstance(plan_snapshot, dict) or not isinstance(scope_snapshot, dict):
        return "rollout_snapshot_invalid", None, plan, scope
    if (
        plan.plan_hash != plan_snapshot.get("plan_hash")
        or plan.resource_version != plan_snapshot.get("resource_version")
        or scope.tool_id != invocation.tool_id
        or scope.descriptor_version != invocation.descriptor_version
        or scope.descriptor_hash != invocation.descriptor_hash
        or scope.provider_id != invocation.selected_provider_id
        or scope.item_id != scope_snapshot.get("item_id")
        or scope.expected_descriptor_resource_version
        != scope_snapshot.get("expected_descriptor_resource_version")
        or scope.expected_provider_resource_version
        != scope_snapshot.get("expected_provider_resource_version")
    ):
        return "rollout_authority_drift", None, plan, scope
    descriptor_record = await session.scalar(
        select(ToolDescriptorRecord)
        .where(ToolDescriptorRecord.id == invocation.descriptor_id)
        .with_for_update()
    )
    if (
        descriptor_record is None
        or descriptor_record.lifecycle != "active"
        or descriptor_record.review_status != "reviewed"
        or descriptor_record.resource_version
        != scope.expected_descriptor_resource_version
        or descriptor_record.descriptor_hash != invocation.descriptor_hash
    ):
        return "descriptor_authority_drift", None, plan, scope
    descriptor = ToolDescriptor.model_validate(descriptor_record.descriptor_json)
    if not descriptor_requires_confirmation(descriptor) or descriptor.confirmation == "two_person":
        return "confirmation_policy_drift", descriptor, plan, scope
    provider = await session.scalar(
        select(ToolProvider)
        .where(ToolProvider.id == invocation.selected_provider_id)
        .with_for_update()
    )
    if (
        provider is None
        or provider.lifecycle != "active"
        or provider.resource_version != scope.expected_provider_resource_version
    ):
        return "provider_authority_drift", descriptor, plan, scope
    registry = await tool_registry_view(session, settings, tool_id=invocation.tool_id)
    registry_version = next(
        (
            item
            for item in registry["tools"]
            if item["version"] == invocation.descriptor_version
            and item["desired"]["descriptor_hash"] == invocation.descriptor_hash
        ),
        None,
    )
    if registry_version is None:
        return "descriptor_runtime_missing", descriptor, plan, scope
    runtime = next(
        (
            item
            for item in registry_version["reported"]
            if item["provider_id"] == invocation.selected_provider_id
            and item["runtime_eligible"]
        ),
        None,
    )
    if runtime is None:
        return "provider_runtime_ineligible", descriptor, plan, scope
    if (
        runtime["inventory_hash"]
        != invocation.policy_snapshot_json.get("selected_inventory_hash")
        or runtime["implementation_hash"]
        != invocation.policy_snapshot_json.get("selected_implementation_hash")
    ):
        return "provider_runtime_drift", descriptor, plan, scope
    try:
        reconstructed = ToolInvocationCreateIn.model_validate(
            {
                "schema_version": "1.0",
                "tool_id": invocation.tool_id,
                "descriptor_version": invocation.descriptor_version,
                "descriptor_hash": invocation.descriptor_hash,
                "input": invocation.input_json,
                "principal": invocation.principal_snapshot_json["facts"],
                "capabilities": invocation.capability_snapshot_json,
            }
        )
    except (KeyError, ValueError):
        return "invocation_snapshot_invalid", descriptor, plan, scope
    if await _rate_limit_exceeded(
        session,
        descriptor,
        reconstructed,
        scope,
        database_time,
        exclude_invocation_id=invocation.id,
    ):
        return "rate_limited", descriptor, plan, scope
    return None, descriptor, plan, scope


async def _decide_tool_confirmation_once(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolInvocationConfirmIn,
    identity: InvocationIdentity,
    idempotency_key: str,
    settings: Settings,
) -> tuple[ToolInvocation, bool]:
    decision_request_hash = _confirmation_decision_hash(invocation_id, payload, identity)
    invocation = await session.scalar(
        select(ToolInvocation)
        .where(ToolInvocation.id == invocation_id)
        .with_for_update()
    )
    if invocation is None:
        raise _not_found()
    if (
        invocation.creator_type != identity.caller
        or invocation.creator_id != identity.subject
    ):
        raise _not_found()
    confirmation = await session.scalar(
        select(ToolConfirmation)
        .where(ToolConfirmation.invocation_id == invocation.id)
        .with_for_update()
    )
    if confirmation is None or confirmation.id != payload.confirmation_id:
        raise _conflict("confirmation does not match this invocation")
    existing = await session.scalar(
        select(ToolConfirmationEvent).where(
            ToolConfirmationEvent.confirmation_id == confirmation.id,
            ToolConfirmationEvent.actor_type == identity.caller,
            ToolConfirmationEvent.actor_id == identity.subject,
            ToolConfirmationEvent.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != decision_request_hash:
            raise _conflict(
                "confirmation idempotency key was reused with different request content"
            )
        if payload.decision == "approve" and existing.event in {"reject", "expire"}:
            raise _conflict("confirmation approval was already rejected")
        refreshed = await session.get(ToolInvocation, invocation.id, populate_existing=True)
        assert refreshed is not None
        return refreshed, True
    if (
        confirmation.request_hash != payload.request_hash
        or confirmation.input_hash != payload.input_hash
        or confirmation.principal_hash != payload.principal_hash
        or confirmation.request_hash != invocation.request_hash
        or confirmation.input_hash != invocation.input_hash
        or confirmation.principal_hash != invocation.principal_hash
        or confirmation.policy_hash != invocation.policy_hash
        or confirmation.caller_type != identity.caller
        or confirmation.caller_id != identity.subject
    ):
        raise _conflict("confirmation hashes or caller do not match immutable authority")
    if invocation.state != "awaiting_confirmation" or confirmation.state != "pending":
        raise _conflict("confirmation is no longer pending")
    database_time = await database_now(session)
    if _aware(confirmation.expires_at) <= database_time:
        await _reject_confirmation_approval(
            session,
            invocation,
            confirmation,
            identity,
            idempotency_key=idempotency_key,
            decision_request_hash=decision_request_hash,
            reason_code="confirmation_expired",
            database_time=database_time,
            expired=True,
        )
        raise _conflict("confirmation expired before approval")
    if payload.decision == "reject":
        await _transition_confirmation(
            session,
            confirmation,
            event="reject",
            state="rejected",
            actor_type=identity.caller,
            actor_id=identity.subject,
            idempotency_key=idempotency_key,
            decision_request_hash=decision_request_hash,
            reason=payload.reason,
            evidence={
                "requested_decision": "reject",
                "decision_request_hash": decision_request_hash,
            },
            effective_at=database_time,
        )
        invocation = await append_invocation_transition(
            session,
            invocation.id,
            event="reject",
            state="rejected",
            actor_type=identity.caller,
            actor_id=identity.subject,
            reason_code="confirmation_rejected",
            evidence={
                "confirmation_id": confirmation.id,
                "decision_request_hash": decision_request_hash,
                "reason": payload.reason,
            },
            commit=False,
        )
        await session.commit()
        return invocation, False

    reason, descriptor, plan, scope = await _approval_revalidation_reason(
        session, invocation, settings, database_time
    )
    if reason is not None or descriptor is None or plan is None or scope is None:
        await _reject_confirmation_approval(
            session,
            invocation,
            confirmation,
            identity,
            idempotency_key=idempotency_key,
            decision_request_hash=decision_request_hash,
            reason_code=reason or "confirmation_revalidation_failed",
            database_time=database_time,
        )
        raise _conflict("confirmation authority changed before approval")
    if not await consume_rollout_invocation(
        session,
        plan,
        database_time=database_time,
    ):
        await _reject_confirmation_approval(
            session,
            invocation,
            confirmation,
            identity,
            idempotency_key=idempotency_key,
            decision_request_hash=decision_request_hash,
            reason_code="rollout_invocation_limit_exhausted",
            database_time=database_time,
        )
        raise _conflict("rollout invocation limit was exhausted before approval")
    execution_deadline = min(
        database_time + timedelta(milliseconds=descriptor.timeout_ms),
        _aware(plan.expires_at),
    )
    await _transition_confirmation(
        session,
        confirmation,
        event="approve",
        state="consumed",
        actor_type=identity.caller,
        actor_id=identity.subject,
        idempotency_key=idempotency_key,
        decision_request_hash=decision_request_hash,
        reason=payload.reason,
        evidence={
            "requested_decision": "approve",
            "decision_request_hash": decision_request_hash,
            "rollout_plan_hash": plan.plan_hash,
            "rollout_plan_resource_version": plan.resource_version,
            "execution_deadline": execution_deadline.isoformat(),
        },
        effective_at=database_time,
    )
    invocation = await append_invocation_transition(
        session,
        invocation.id,
        event="confirm",
        state="queued",
        actor_type=identity.caller,
        actor_id=identity.subject,
        reason_code="confirmation_approved",
        evidence={
            "confirmation_id": confirmation.id,
            "decision_request_hash": decision_request_hash,
            "rollout_plan_hash": plan.plan_hash,
            "rollout_plan_resource_version": plan.resource_version,
            "rollout_plan_item_id": scope.item_id,
        },
        deadline_at=execution_deadline,
        commit=False,
    )
    await session.commit()
    return invocation, False


async def decide_tool_confirmation(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolInvocationConfirmIn,
    identity: InvocationIdentity,
    idempotency_key: str,
    settings: Settings,
) -> tuple[ToolInvocation, bool]:
    decision_request_hash = _confirmation_decision_hash(invocation_id, payload, identity)
    try:
        return await _decide_tool_confirmation_once(
            session,
            invocation_id,
            payload,
            identity,
            idempotency_key,
            settings,
        )
    except IntegrityError as exc:
        await session.rollback()
        existing = await session.scalar(
            select(ToolConfirmationEvent)
            .join(
                ToolConfirmation,
                ToolConfirmation.id == ToolConfirmationEvent.confirmation_id,
            )
            .where(
                ToolConfirmation.invocation_id == invocation_id,
                ToolConfirmationEvent.actor_type == identity.caller,
                ToolConfirmationEvent.actor_id == identity.subject,
                ToolConfirmationEvent.idempotency_key == idempotency_key,
            )
        )
        if existing is not None and existing.request_hash == decision_request_hash:
            if payload.decision == "approve" and existing.event in {"reject", "expire"}:
                raise _conflict("confirmation approval was already rejected") from exc
            invocation = await session.get(ToolInvocation, invocation_id)
            if (
                invocation is not None
                and invocation.creator_type == identity.caller
                and invocation.creator_id == identity.subject
            ):
                return invocation, True
        if existing is not None:
            raise _conflict(
                "confirmation idempotency key was reused with different request content"
            ) from exc
        raise _conflict("concurrent confirmation decision conflict") from exc


async def cancel_tool_invocation(
    session: AsyncSession,
    invocation_id: str,
    identity: InvocationIdentity,
    reason: str,
) -> ToolInvocation:
    invocation = await session.scalar(
        select(ToolInvocation)
        .where(ToolInvocation.id == invocation_id)
        .with_for_update()
    )
    if invocation is None:
        raise _not_found()
    _authorize_invocation(invocation, identity)
    if invocation.state in TERMINAL_INVOCATION_STATES:
        raise _conflict("terminal invocation is immutable")
    was_awaiting_confirmation = invocation.state == "awaiting_confirmation"
    if invocation.state in {"leased", "running"}:
        event: InvocationTransitionEvent = "request_cancel"
        next_state: InvocationState = "cancel_requested"
    elif invocation.state in {"proposed", "awaiting_confirmation", "queued"}:
        event = "cancel"
        next_state = "cancelled"
    else:
        raise _conflict("invocation cannot be cancelled from its current state")
    if was_awaiting_confirmation:
        confirmation = await session.scalar(
            select(ToolConfirmation)
            .where(ToolConfirmation.invocation_id == invocation.id)
            .with_for_update()
        )
        if confirmation is None:
            raise _conflict("pending invocation has no confirmation authority")
        database_time = await database_now(session)
        cancellation_hash = canonicalize_json_value(
            {
                "invocation_id": invocation.id,
                "confirmation_id": confirmation.id,
                "caller": identity.caller,
                "subject": identity.subject,
                "reason": reason,
            }
        ).sha256
        await _transition_confirmation(
            session,
            confirmation,
            event="reject",
            state="rejected",
            actor_type=identity.caller,
            actor_id=identity.subject,
            idempotency_key=f"cancel:{invocation.id}:{invocation.transition_sequence + 1}",
            decision_request_hash=cancellation_hash,
            reason=reason,
            evidence={
                "operation": "cancel",
                "cancellation_hash": cancellation_hash,
            },
            effective_at=database_time,
        )
    result = await append_invocation_transition(
        session,
        invocation.id,
        event=event,
        state=next_state,
        actor_type=identity.caller,
        actor_id=identity.subject,
        reason_code="caller_cancelled",
        evidence={"reason": reason},
        commit=not was_awaiting_confirmation,
    )
    if was_awaiting_confirmation:
        await session.commit()
    return result


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
            confirmation = await session.scalar(
                select(ToolConfirmation)
                .where(ToolConfirmation.invocation_id == invocation.id)
                .with_for_update()
            )
            if confirmation is None or confirmation.state != "pending":
                raise RuntimeError(
                    "awaiting_confirmation invocation lacks one pending confirmation"
                )
            expiry_hash = canonicalize_json_value(
                {
                    "operation": "expire",
                    "invocation_id": invocation.id,
                    "confirmation_id": confirmation.id,
                    "database_time": now.isoformat(),
                }
            ).sha256
            await _transition_confirmation(
                session,
                confirmation,
                event="expire",
                state="expired",
                actor_type="reaper",
                actor_id="tool-invocation-reaper-v1",
                idempotency_key=f"expire:{confirmation.id}:{confirmation.resource_version + 1}",
                decision_request_hash=expiry_hash,
                reason="确认等待期已过期",
                evidence={
                    "operation": "expire",
                    "database_time": now.isoformat(),
                },
                effective_at=now,
            )
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
