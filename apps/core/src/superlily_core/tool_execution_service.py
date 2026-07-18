"""Provider 拉取 lease、fence 校验和 attempt 恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    ToolDescriptor,
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionProof,
    ToolExecutionStartIn,
    ToolLeaseOut,
    ToolLeaseRequestIn,
    ToolRegistryContractError,
    ToolUsage,
    canonicalize_json_value,
    lease_secret_hash,
    validate_schema_instance,
)

from .models import (
    ToolAttempt,
    ToolAttemptEvent,
    ToolDescriptorRecord,
    ToolInvocation,
    ToolProvider,
    ToolProviderCredential,
    new_id,
)
from .rollout_service import locked_rollout_for_lease
from .settings import Settings
from .tool_invocation_service import append_invocation_transition, database_now
from .tool_registry_service import tool_registry_view


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool attempt not found")


def _snapshot(value: Any) -> tuple[Any, str]:
    canonical = canonicalize_json_value(value)
    return canonical.value, canonical.sha256


def _usage_payload(usage: ToolUsage) -> tuple[dict[str, int], str]:
    value, digest = _snapshot(usage.model_dump(mode="json"))
    return value, digest


def _attempt_view(attempt: ToolAttempt) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "attempt_id": attempt.id,
        "invocation_id": attempt.invocation_id,
        "attempt_number": attempt.attempt_number,
        "provider_id": attempt.provider_id,
        "inventory_hash": attempt.inventory_hash,
        "implementation_hash": attempt.implementation_hash,
        "fencing_token": attempt.fencing_token,
        "state": attempt.state,
        "deadline_at": attempt.deadline_at,
        "lease_expires_at": attempt.lease_expires_at,
        "started_at": attempt.started_at,
        "last_heartbeat_at": attempt.last_heartbeat_at,
        "completed_at": attempt.completed_at,
        "usage": attempt.usage_json,
        "output": attempt.output_json,
        "output_hash": attempt.output_hash,
        "provider_result_id": attempt.provider_result_id,
        "error_code": attempt.error_code,
        "safe_error_detail": attempt.safe_error_detail,
        "created_at": attempt.created_at,
        "updated_at": attempt.updated_at,
    }


async def attempt_views(session: AsyncSession, invocation_id: str) -> list[dict[str, Any]]:
    attempts = (
        await session.scalars(
            select(ToolAttempt)
            .where(ToolAttempt.invocation_id == invocation_id)
            .order_by(ToolAttempt.attempt_number)
        )
    ).all()
    return [_attempt_view(item) for item in attempts]


async def _append_attempt_event(
    session: AsyncSession,
    attempt: ToolAttempt,
    *,
    event: str,
    outcome: str,
    provider_id: str,
    fencing_token: int,
    reason_code: str,
    evidence: dict[str, Any],
    now: datetime,
) -> None:
    value, digest = _snapshot(evidence)
    attempt.event_sequence += 1
    attempt.updated_at = now
    session.add(
        ToolAttemptEvent(
            attempt_id=attempt.id,
            sequence=attempt.event_sequence,
            event=event,
            outcome=outcome,
            provider_id=provider_id,
            fencing_token=fencing_token,
            reason_code=reason_code,
            evidence_json=value,
            evidence_hash=digest,
            created_at=now,
        )
    )


async def _reject_known_attempt(
    session: AsyncSession,
    attempt: ToolAttempt,
    *,
    provider_id: str,
    fencing_token: int,
    operation: str,
    reason_code: str,
    detail: str,
    now: datetime,
) -> None:
    await _append_attempt_event(
        session,
        attempt,
        event="reject",
        outcome="rejected",
        provider_id=provider_id,
        fencing_token=max(1, fencing_token),
        reason_code=reason_code,
        evidence={"operation": operation, "attempt_state": attempt.state},
        now=now,
    )
    await session.commit()
    raise _conflict(detail)


async def _locked_attempt_and_invocation(
    session: AsyncSession,
    invocation_id: str,
    proof: ToolExecutionProof,
    provider_id: str,
    *,
    operation: str,
    allowed_attempt_states: set[str],
    allowed_invocation_states: set[str],
) -> tuple[ToolAttempt, ToolInvocation, datetime]:
    now = await database_now(session)
    attempt = await session.scalar(
        select(ToolAttempt).where(ToolAttempt.id == proof.attempt_id).with_for_update()
    )
    if attempt is None or attempt.invocation_id != invocation_id:
        raise _not_found()
    invocation = await session.scalar(
        select(ToolInvocation)
        .where(ToolInvocation.id == invocation_id)
        .with_for_update()
    )
    if invocation is None:
        raise _not_found()
    checks = (
        (attempt.provider_id == provider_id, "provider_mismatch", "attempt belongs to another provider"),
        (
            attempt.fencing_token == proof.fencing_token,
            "stale_fence",
            "fencing token is stale or invalid",
        ),
        (
            secrets.compare_digest(attempt.lease_secret_hash, lease_secret_hash(proof.lease_secret)),
            "secret_mismatch",
            "attempt secret is invalid",
        ),
        (
            attempt.state in allowed_attempt_states,
            "attempt_state_conflict",
            "attempt is not in a state accepted by this operation",
        ),
        (
            invocation.state in allowed_invocation_states,
            "invocation_state_conflict",
            "invocation is not in a state accepted by this operation",
        ),
        (
            _aware(attempt.lease_expires_at) >= now,
            "lease_expired",
            "attempt lease has expired",
        ),
        (
            _aware(attempt.deadline_at) >= now,
            "deadline_expired",
            "invocation deadline has expired",
        ),
    )
    for accepted, reason_code, detail in checks:
        if not accepted:
            await _reject_known_attempt(
                session,
                attempt,
                provider_id=provider_id,
                fencing_token=proof.fencing_token,
                operation=operation,
                reason_code=reason_code,
                detail=detail,
                now=now,
            )
    return attempt, invocation, now


def _selected_runtime(
    registry_version: dict[str, Any],
    provider_id: str,
    inventory_hash: str,
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in registry_version["reported"]
            if item["provider_id"] == provider_id
            and item["runtime_eligible"]
            and item["inventory_hash"] == inventory_hash
            and item["implementation_hash"] is not None
        ),
        None,
    )


async def lease_tool_execution(
    session: AsyncSession,
    payload: ToolLeaseRequestIn,
    provider_id: str,
    settings: Settings,
) -> ToolLeaseOut | None:
    """拉取一个最早的精确匹配调用；无工作时返回 None。"""

    if settings.tool_execution_mode != "canary" or settings.tool_global_stop:
        return None
    now = await database_now(session)
    provider = await session.scalar(
        select(ToolProvider)
        .where(ToolProvider.id == provider_id)
        .with_for_update()
    )
    if provider is None or provider.lifecycle != "active":
        return None
    candidates = list(
        (
            await session.scalars(
                select(ToolInvocation)
                .where(
                    ToolInvocation.state == "queued",
                    ToolInvocation.selected_provider_id == provider_id,
                    ToolInvocation.deadline_at > now,
                )
                .order_by(ToolInvocation.created_at, ToolInvocation.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    for invocation in candidates:
        policy = invocation.policy_snapshot_json
        if invocation.rollout_plan_id is None or invocation.rollout_plan_item_id is None:
            continue
        rollout = await locked_rollout_for_lease(
            session,
            plan_record_id=invocation.rollout_plan_id,
            plan_item_id=invocation.rollout_plan_item_id,
            database_time=now,
        )
        if rollout is None:
            continue
        plan, plan_item = rollout
        plan_snapshot = policy.get("rollout_plan") or {}
        if (
            invocation.execution_mode != "canary"
            or plan_snapshot.get("plan_hash") != plan.plan_hash
            or plan_snapshot.get("resource_version") != plan.resource_version
            or plan_item.tool_id != invocation.tool_id
            or plan_item.descriptor_version != invocation.descriptor_version
            or plan_item.descriptor_hash != invocation.descriptor_hash
            or plan_item.canonical_conversation != policy.get("canonical_conversation")
            or plan_item.caller != invocation.creator_type
            or plan_item.provider_id != provider_id
            or provider.resource_version != plan_item.expected_provider_resource_version
        ):
            continue
        descriptor_authority = await session.get(ToolDescriptorRecord, invocation.descriptor_id)
        if (
            descriptor_authority is None
            or descriptor_authority.lifecycle != "active"
            or descriptor_authority.resource_version
            != plan_item.expected_descriptor_resource_version
        ):
            continue
        credential = await session.get(ToolProviderCredential, provider_id)
        if credential is None or credential.lifecycle != "active":
            continue
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
        if registry_version is None or not registry_version["effective"]["eligible"]:
            continue
        runtime = _selected_runtime(registry_version, provider_id, payload.inventory_hash)
        if runtime is None:
            continue
        if policy.get("selected_implementation_hash") != runtime["implementation_hash"]:
            continue

        provider_view = next(
            (item for item in registry["providers"] if item["provider_id"] == provider_id),
            None,
        )
        heartbeat = (
            None if provider_view is None else provider_view["reported"].get("heartbeat")
        )
        if heartbeat is None:
            continue
        active_provider_attempts = await session.scalar(
            select(func.count(ToolAttempt.id)).where(
                ToolAttempt.provider_id == provider_id,
                ToolAttempt.state.in_({"leased", "running"}),
            )
        )
        if max(int(active_provider_attempts or 0), int(heartbeat["current_concurrency"])) >= int(
            heartbeat["max_concurrency"]
        ):
            continue

        descriptor = ToolDescriptor.model_validate(invocation.descriptor_snapshot_json)
        active_tool_attempts = await session.scalar(
            select(func.count(ToolAttempt.id))
            .join(ToolInvocation, ToolInvocation.id == ToolAttempt.invocation_id)
            .where(
                ToolInvocation.tool_id == invocation.tool_id,
                ToolInvocation.descriptor_version == invocation.descriptor_version,
                ToolAttempt.state.in_({"leased", "running"}),
            )
        )
        if int(active_tool_attempts or 0) >= descriptor.concurrency_limit:
            continue

        attempt_count = await session.scalar(
            select(func.count(ToolAttempt.id)).where(
                ToolAttempt.invocation_id == invocation.id
            )
        )
        max_fence = await session.scalar(
            select(func.max(ToolAttempt.fencing_token)).where(
                ToolAttempt.invocation_id == invocation.id
            )
        )
        attempt_number = int(attempt_count or 0) + 1
        fence = int(max_fence or 0) + 1
        secret = secrets.token_urlsafe(32)
        lease_expires_at = min(
            _aware(invocation.deadline_at),
            now + timedelta(seconds=settings.tool_lease_seconds),
        )
        budget, budget_hash = _snapshot(policy["resource_budget"])
        permissions, permissions_hash = _snapshot(policy["execution_permissions"])
        usage, usage_hash = _usage_payload(ToolUsage())
        attempt = ToolAttempt(
            id=new_id(),
            invocation_id=invocation.id,
            attempt_number=attempt_number,
            provider_id=provider_id,
            inventory_hash=payload.inventory_hash,
            implementation_hash=runtime["implementation_hash"],
            fencing_token=fence,
            lease_secret_hash=lease_secret_hash(secret),
            state="leased",
            deadline_at=_aware(invocation.deadline_at),
            lease_expires_at=lease_expires_at,
            budget_snapshot_json=budget,
            budget_hash=budget_hash,
            permissions_snapshot_json=permissions,
            permissions_hash=permissions_hash,
            usage_json=usage,
            usage_hash=usage_hash,
            event_sequence=1,
            created_at=now,
            updated_at=now,
        )
        invocation_id = invocation.id
        session.add(attempt)
        try:
            await session.flush()
        except IntegrityError as exc:
            # SQLite 没有可用的 SKIP LOCKED；并发 pull 依靠单活动
            # attempt/序号唯一约束收敛。输家不获得 secret，只看到无工作。
            await session.rollback()
            active = await session.scalar(
                select(ToolAttempt).where(
                    ToolAttempt.invocation_id == invocation_id,
                    ToolAttempt.state.in_({"leased", "running"}),
                )
            )
            if active is not None:
                return None
            raise exc
        lease_evidence = {
            "invocation_id": invocation.id,
            "attempt_number": attempt_number,
            "inventory_hash": payload.inventory_hash,
            "implementation_hash": runtime["implementation_hash"],
            "budget_hash": budget_hash,
            "permissions_hash": permissions_hash,
            "lease_expires_at": lease_expires_at.isoformat(),
            "deadline_at": _aware(invocation.deadline_at).isoformat(),
        }
        evidence_value, evidence_hash = _snapshot(lease_evidence)
        session.add(
            ToolAttemptEvent(
                attempt_id=attempt.id,
                sequence=1,
                event="lease",
                outcome="accepted",
                provider_id=provider_id,
                fencing_token=fence,
                reason_code="lease_issued",
                evidence_json=evidence_value,
                evidence_hash=evidence_hash,
                created_at=now,
            )
        )
        await append_invocation_transition(
            session,
            invocation.id,
            event="lease",
            state="leased",
            actor_type="provider",
            actor_id=provider_id,
            reason_code="lease_issued",
            evidence={
                "attempt_id": attempt.id,
                "attempt_number": attempt_number,
                "fencing_token": fence,
                **lease_evidence,
            },
            commit=False,
        )
        lease = ToolLeaseOut(
            invocation_id=invocation.id,
            attempt_id=attempt.id,
            attempt_number=attempt_number,
            fencing_token=fence,
            lease_secret=secret,
            provider_id=provider_id,
            inventory_hash=payload.inventory_hash,
            implementation_hash=runtime["implementation_hash"],
            tool_id=invocation.tool_id,
            descriptor_version=invocation.descriptor_version,
            descriptor_hash=invocation.descriptor_hash,
            input=invocation.input_json,
            input_hash=invocation.input_hash,
            deadline_at=_aware(invocation.deadline_at),
            lease_expires_at=lease_expires_at,
            resource_budget=budget,
            execution_permissions=permissions,
        )
        await session.commit()
        return lease
    return None


async def start_tool_execution(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolExecutionStartIn,
    provider_id: str,
) -> dict[str, Any]:
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="start",
        allowed_attempt_states={"leased"},
        allowed_invocation_states={"leased"},
    )
    await append_invocation_transition(
        session,
        invocation.id,
        event="start",
        state="running",
        actor_type="provider",
        actor_id=provider_id,
        reason_code="provider_started",
        evidence={"attempt_id": attempt.id, "fencing_token": attempt.fencing_token},
        commit=False,
    )
    attempt.state = "running"
    attempt.started_at = now
    attempt.last_heartbeat_at = now
    await _append_attempt_event(
        session,
        attempt,
        event="start",
        outcome="accepted",
        provider_id=provider_id,
        fencing_token=attempt.fencing_token,
        reason_code="provider_started",
        evidence={"database_time": now.isoformat()},
        now=now,
    )
    await session.commit()
    return _attempt_view(attempt)


async def heartbeat_tool_execution(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolExecutionHeartbeatIn,
    provider_id: str,
    settings: Settings,
) -> dict[str, Any]:
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="heartbeat",
        allowed_attempt_states={"running"},
        allowed_invocation_states={"running", "cancel_requested"},
    )
    usage, usage_hash = _usage_payload(payload.usage)
    attempt.usage_json = usage
    attempt.usage_hash = usage_hash
    attempt.last_heartbeat_at = now
    attempt.lease_expires_at = min(
        _aware(attempt.deadline_at),
        now + timedelta(seconds=settings.tool_lease_seconds),
    )
    descriptor = ToolDescriptor.model_validate(invocation.descriptor_snapshot_json)
    live_limits = {
        "wall_time": (payload.usage.wall_time_ms, descriptor.timeout_ms),
        "cpu": (payload.usage.cpu_ms, descriptor.resource_budget.cpu_ms),
        "memory": (
            payload.usage.memory_peak_bytes,
            descriptor.resource_budget.memory_bytes,
        ),
        "input_bytes": (
            payload.usage.input_bytes,
            descriptor.resource_budget.input_bytes,
        ),
        "output_bytes": (
            payload.usage.output_bytes,
            descriptor.resource_budget.output_bytes,
        ),
        "artifact_bytes": (
            payload.usage.artifact_bytes,
            descriptor.resource_budget.artifact_bytes,
        ),
    }
    violation = next(
        (
            name
            for name, (value, limit) in live_limits.items()
            if limit is not None and value > limit
        ),
        None,
    )
    if violation is not None and invocation.state == "running":
        invocation = await append_invocation_transition(
            session,
            invocation.id,
            event="request_cancel",
            state="cancel_requested",
            actor_type="system",
            actor_id="tool-budget-v1",
            reason_code="budget_exceeded",
            evidence={"attempt_id": attempt.id, "budget_name": violation},
            commit=False,
        )
    await _append_attempt_event(
        session,
        attempt,
        event="heartbeat",
        outcome="accepted",
        provider_id=provider_id,
        fencing_token=attempt.fencing_token,
        reason_code=("budget_exceeded" if violation is not None else "heartbeat_accepted"),
        evidence={
            "usage_hash": usage_hash,
            "lease_expires_at": attempt.lease_expires_at.isoformat(),
            "provider_observed_at": (
                None
                if payload.provider_observed_at is None
                else payload.provider_observed_at.isoformat()
            ),
            "budget_violation": violation,
        },
        now=now,
    )
    await session.commit()
    result = _attempt_view(attempt)
    result["cancel_requested"] = invocation.state == "cancel_requested"
    return result


def _budget_violation(
    descriptor: ToolDescriptor,
    usage: ToolUsage,
    *,
    actual_input_bytes: int,
    actual_output_bytes: int,
) -> str | None:
    if usage.input_bytes != actual_input_bytes or usage.output_bytes != actual_output_bytes:
        return "usage_mismatch"
    budget = descriptor.resource_budget
    limits = {
        "cpu": (usage.cpu_ms, budget.cpu_ms),
        "memory": (usage.memory_peak_bytes, budget.memory_bytes),
        "input_bytes": (actual_input_bytes, budget.input_bytes),
        "output_bytes": (actual_output_bytes, budget.output_bytes),
        "artifact_bytes": (usage.artifact_bytes, budget.artifact_bytes),
        "wall_time": (usage.wall_time_ms, descriptor.timeout_ms),
    }
    return next(
        (name for name, (value, limit) in limits.items() if limit is not None and value > limit),
        None,
    )


async def _finish_attempt(
    session: AsyncSession,
    attempt: ToolAttempt,
    invocation: ToolInvocation,
    *,
    provider_id: str,
    provider_result_id: str,
    usage: ToolUsage,
    event: str,
    invocation_event: str,
    invocation_state: str,
    attempt_state: str,
    reason_code: str,
    evidence: dict[str, Any],
    now: datetime,
    output: Any | None = None,
    output_hash: str | None = None,
    error_code: str | None = None,
    safe_error_detail: str | None = None,
) -> dict[str, Any]:
    await append_invocation_transition(
        session,
        invocation.id,
        event=invocation_event,  # type: ignore[arg-type]
        state=invocation_state,  # type: ignore[arg-type]
        actor_type="provider",
        actor_id=provider_id,
        reason_code=reason_code,
        evidence={"attempt_id": attempt.id, "fencing_token": attempt.fencing_token, **evidence},
        commit=False,
    )
    usage_value, usage_hash = _usage_payload(usage)
    attempt.state = attempt_state
    attempt.completed_at = now
    attempt.last_heartbeat_at = now
    attempt.usage_json = usage_value
    attempt.usage_hash = usage_hash
    attempt.output_json = output
    attempt.output_hash = output_hash
    attempt.provider_result_id = provider_result_id
    attempt.error_code = error_code
    attempt.safe_error_detail = safe_error_detail
    await _append_attempt_event(
        session,
        attempt,
        event=event,
        outcome="accepted",
        provider_id=provider_id,
        fencing_token=attempt.fencing_token,
        reason_code=reason_code,
        evidence={"usage_hash": usage_hash, **evidence},
        now=now,
    )
    await session.commit()
    return _attempt_view(attempt)


async def complete_tool_execution(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolExecutionCompleteIn,
    provider_id: str,
) -> dict[str, Any]:
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="complete",
        allowed_attempt_states={"running"},
        allowed_invocation_states={"running", "cancel_requested"},
    )
    descriptor = ToolDescriptor.model_validate(invocation.descriptor_snapshot_json)
    output_value, output_hash = _snapshot(payload.output)
    input_bytes = len(canonicalize_json_value(invocation.input_json).canonical_bytes)
    output_bytes = len(canonicalize_json_value(output_value).canonical_bytes)
    try:
        validate_schema_instance(output_value, descriptor.output_schema)
    except ToolRegistryContractError:
        cancelled_race = invocation.state == "cancel_requested"
        return await _finish_attempt(
            session,
            attempt,
            invocation,
            provider_id=provider_id,
            provider_result_id=payload.provider_result_id,
            usage=payload.usage,
            event="fail",
            invocation_event=("unknown_completion" if cancelled_race else "complete_failure"),
            invocation_state=("unknown_completion" if cancelled_race else "failed"),
            attempt_state=("unknown_completion" if cancelled_race else "failed"),
            reason_code=("completion_raced_cancellation" if cancelled_race else "invalid_output"),
            evidence={"output_hash": output_hash},
            now=now,
            output_hash=output_hash,
            error_code="invalid_output",
            safe_error_detail="provider output did not satisfy the reviewed schema",
        )
    violation = _budget_violation(
        descriptor,
        payload.usage,
        actual_input_bytes=input_bytes,
        actual_output_bytes=output_bytes,
    )
    if violation is not None:
        cancelled_race = invocation.state == "cancel_requested"
        return await _finish_attempt(
            session,
            attempt,
            invocation,
            provider_id=provider_id,
            provider_result_id=payload.provider_result_id,
            usage=payload.usage,
            event="fail",
            invocation_event=("unknown_completion" if cancelled_race else "complete_failure"),
            invocation_state=("unknown_completion" if cancelled_race else "failed"),
            attempt_state=("unknown_completion" if cancelled_race else "failed"),
            reason_code=("completion_raced_cancellation" if cancelled_race else "budget_exceeded"),
            evidence={"budget_name": violation, "output_hash": output_hash},
            now=now,
            output_hash=output_hash,
            error_code="budget_exceeded",
            safe_error_detail="provider result exceeded or misreported its reviewed budget",
        )
    if invocation.state == "cancel_requested":
        return await _finish_attempt(
            session,
            attempt,
            invocation,
            provider_id=provider_id,
            provider_result_id=payload.provider_result_id,
            usage=payload.usage,
            event="complete",
            invocation_event="unknown_completion",
            invocation_state="unknown_completion",
            attempt_state="unknown_completion",
            reason_code="completion_raced_cancellation",
            evidence={"output_hash": output_hash},
            now=now,
            output_hash=output_hash,
            error_code="cancelled",
            safe_error_detail="completion arrived after cancellation was requested",
        )
    return await _finish_attempt(
        session,
        attempt,
        invocation,
        provider_id=provider_id,
        provider_result_id=payload.provider_result_id,
        usage=payload.usage,
        event="complete",
        invocation_event="complete_success",
        invocation_state="succeeded",
        attempt_state="succeeded",
        reason_code="provider_completed",
        evidence={"output_hash": output_hash},
        now=now,
        output=output_value,
        output_hash=output_hash,
    )


async def fail_tool_execution(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolExecutionFailIn,
    provider_id: str,
) -> dict[str, Any]:
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="fail",
        # 已领取但尚未 start 时，调用方可能先发出取消；Provider 必须能
        # 直接确认该取消，不能被迫等 lease 过期后落成 unknown。
        allowed_attempt_states={"leased", "running"},
        allowed_invocation_states={"running", "cancel_requested"},
    )
    if invocation.state == "cancel_requested" and payload.error_code == "cancelled":
        invocation_event = "cancel"
        invocation_state = "cancelled"
        attempt_state = "cancelled"
        attempt_event = "cancel"
        reason_code = "provider_acknowledged_cancellation"
    elif invocation.state == "cancel_requested":
        invocation_event = "unknown_completion"
        invocation_state = "unknown_completion"
        attempt_state = "unknown_completion"
        attempt_event = "fail"
        reason_code = "failure_raced_cancellation"
    else:
        invocation_event = "complete_failure"
        invocation_state = "failed"
        attempt_state = "failed"
        attempt_event = "fail"
        reason_code = payload.error_code
    return await _finish_attempt(
        session,
        attempt,
        invocation,
        provider_id=provider_id,
        provider_result_id=payload.provider_result_id,
        usage=payload.usage,
        event=attempt_event,
        invocation_event=invocation_event,
        invocation_state=invocation_state,
        attempt_state=attempt_state,
        reason_code=reason_code,
        evidence={"error_code": payload.error_code},
        now=now,
        error_code=payload.error_code,
        safe_error_detail=payload.safe_detail,
    )


async def reap_expired_attempts(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> list[str]:
    now = await database_now(session)
    attempts = list(
        (
            await session.scalars(
                select(ToolAttempt)
                .where(
                    ToolAttempt.state.in_({"leased", "running"}),
                    ToolAttempt.lease_expires_at <= now,
                )
                .order_by(ToolAttempt.lease_expires_at, ToolAttempt.created_at)
                .limit(max(1, min(limit, 1_000)))
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    reaped: list[str] = []
    for attempt in attempts:
        invocation = await session.scalar(
            select(ToolInvocation)
            .where(ToolInvocation.id == attempt.invocation_id)
            .with_for_update()
        )
        if invocation is None:
            continue
        original_invocation_state = invocation.state
        attempt.state = "lease_expired"
        attempt.completed_at = now
        if original_invocation_state in {"leased", "running"}:
            await append_invocation_transition(
                session,
                invocation.id,
                event="lease_expire",
                state="lease_expired",
                actor_type="reaper",
                actor_id="tool-attempt-reaper-v1",
                reason_code="lease_expired",
                evidence={"attempt_id": attempt.id, "fencing_token": attempt.fencing_token},
                commit=False,
            )
            descriptor = ToolDescriptor.model_validate(invocation.descriptor_snapshot_json)
            if descriptor.retry_policy == "retry_safe" and _aware(invocation.deadline_at) > now:
                await append_invocation_transition(
                    session,
                    invocation.id,
                    event="requeue",
                    state="queued",
                    actor_type="reaper",
                    actor_id="tool-attempt-reaper-v1",
                    reason_code="safe_retry_after_lease_expiry",
                    evidence={"expired_attempt_id": attempt.id},
                    commit=False,
                )
            elif descriptor.retry_policy == "retry_safe":
                await append_invocation_transition(
                    session,
                    invocation.id,
                    event="timeout",
                    state="timed_out",
                    actor_type="reaper",
                    actor_id="tool-attempt-reaper-v1",
                    reason_code="deadline_expired",
                    evidence={"expired_attempt_id": attempt.id},
                    commit=False,
                )
            else:
                await append_invocation_transition(
                    session,
                    invocation.id,
                    event="unknown_completion",
                    state="unknown_completion",
                    actor_type="reaper",
                    actor_id="tool-attempt-reaper-v1",
                    reason_code="ambiguous_lease_expiry",
                    evidence={"expired_attempt_id": attempt.id},
                    commit=False,
                )
        elif original_invocation_state == "cancel_requested":
            await append_invocation_transition(
                session,
                invocation.id,
                event="unknown_completion",
                state="unknown_completion",
                actor_type="reaper",
                actor_id="tool-attempt-reaper-v1",
                reason_code="cancellation_unacknowledged",
                evidence={"expired_attempt_id": attempt.id},
                commit=False,
            )
        await _append_attempt_event(
            session,
            attempt,
            event="lease_expire",
            outcome="accepted",
            provider_id=attempt.provider_id,
            fencing_token=attempt.fencing_token,
            reason_code="lease_expired",
            evidence={"database_time": now.isoformat()},
            now=now,
        )
        reaped.append(attempt.id)
    await session.commit()
    return reaped
