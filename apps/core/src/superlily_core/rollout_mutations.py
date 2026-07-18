"""M3 rollout plan 的 operator preview、CAS、暂停和只追加证据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import ToolDescriptor, canonicalize_json_value

from .control_plane import (
    ControlSessionIdentity,
    append_control_audit,
    authenticate_control_session,
)
from .models import (
    ControlPlaneAuditEvent,
    ControlPlaneMutation,
    ControlPlanePreview,
    ToolDescriptorRecord,
    ToolInvocation,
    ToolProvider,
    ToolProviderCredential,
    ToolProviderHeartbeat,
    ToolProviderInventoryEntry,
    ToolProviderInventorySnapshot,
    ToolRolloutPlanCounter,
    ToolRolloutPlanItemRecord,
    ToolRolloutPlanLifecycleEvent,
    ToolRolloutPlanRecord,
    new_id,
)
from .settings import Settings


_OPERATION = "rollout_plan_lifecycle_apply"
_TARGET_TYPE = "tool_rollout_plan"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_LEGAL_TRANSITIONS = {
    ("reviewed", "active"),
    ("active", "paused"),
    ("paused", "active"),
}


class _RolloutMutationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RolloutPlanLifecyclePreviewIn(_RolloutMutationModel):
    schema_version: Literal["1.0"] = "1.0"
    plan_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9.-]{2,127}$",
    )
    version: str = Field(
        min_length=5,
        max_length=64,
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"
        ),
    )
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_lifecycle: Literal["active", "paused"]


class RolloutPlanLifecycleApplyIn(RolloutPlanLifecyclePreviewIn):
    preview_id: str = Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_version: int = Field(ge=1, le=2_147_483_647)
    reason: str = Field(min_length=8, max_length=512)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _aware(value).isoformat()


def _target_id(payload: RolloutPlanLifecyclePreviewIn) -> str:
    return f"{payload.plan_id}@{payload.version}#{payload.plan_hash}"


def _preview_request(payload: RolloutPlanLifecyclePreviewIn) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "plan_id": payload.plan_id,
        "version": payload.version,
        "plan_hash": payload.plan_hash,
        "desired_lifecycle": payload.desired_lifecycle,
    }


def _request_hash(value: dict[str, Any]) -> str:
    return canonicalize_json_value(value).sha256


def _exact_reason(value: str) -> str:
    if value != value.strip() or not 8 <= len(value) <= 512:
        raise HTTPException(
            status_code=422,
            detail="control mutation reason must be exact and between 8 and 512 characters",
        )
    return value


def _idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail="invalid control Idempotency-Key")
    return value


async def _authorize_operator(
    session: AsyncSession,
    request: Request,
    settings: Settings,
    *,
    event: str,
    desired_lifecycle: str,
    require_fresh: bool,
) -> tuple[ControlSessionIdentity, datetime]:
    _, identity, now = await authenticate_control_session(
        session,
        request,
        settings,
        require_origin=True,
        require_json=True,
        require_csrf=True,
        for_update=False,
    )
    allowed = identity.role == "operator" or (
        identity.role == "break_glass" and desired_lifecycle == "paused"
    )
    if not allowed:
        await append_control_audit(
            session,
            event=event,
            outcome="rejected",
            reason_code="role_forbidden",
            evidence={
                "required_role": "operator",
                "break_glass_pause_only": True,
                "desired_lifecycle": desired_lifecycle,
            },
            identity=identity,
        )
        await session.commit()
        raise HTTPException(status_code=403, detail="control role is not authorized")
    if require_fresh and (
        identity.last_reauthenticated_at
        + timedelta(seconds=settings.control_reauth_seconds)
        <= now
    ):
        await append_control_audit(
            session,
            event=event,
            outcome="rejected",
            reason_code="reauthentication_required",
            evidence={"session_version": identity.resource_version},
            identity=identity,
        )
        await session.commit()
        raise HTTPException(status_code=403, detail="fresh reauthentication is required")
    return identity, now


async def _enforce_mutation_rate(
    session: AsyncSession,
    identity: ControlSessionIdentity,
    now: datetime,
    settings: Settings,
) -> None:
    cutoff = now - timedelta(seconds=settings.control_mutation_window_seconds)
    attempts = await session.scalar(
        select(func.count(ControlPlaneAuditEvent.id)).where(
            ControlPlaneAuditEvent.operator_id == identity.operator_id,
            ControlPlaneAuditEvent.event == _OPERATION,
            ControlPlaneAuditEvent.reason_code != "rate_limited",
            ControlPlaneAuditEvent.created_at >= cutoff,
        )
    )
    if int(attempts or 0) < settings.control_mutation_attempts:
        return
    await append_control_audit(
        session,
        event=_OPERATION,
        outcome="rejected",
        reason_code="rate_limited",
        evidence={"window_seconds": settings.control_mutation_window_seconds},
        identity=identity,
    )
    await session.commit()
    raise HTTPException(status_code=429, detail="control mutation rate limited")


async def _enforce_preview_rate(
    session: AsyncSession,
    identity: ControlSessionIdentity,
    now: datetime,
    settings: Settings,
) -> None:
    cutoff = now - timedelta(seconds=settings.control_mutation_window_seconds)
    previews = await session.scalar(
        select(func.count(ControlPlanePreview.id)).where(
            ControlPlanePreview.session_id == identity.session_id,
            ControlPlanePreview.operation == _OPERATION,
            ControlPlanePreview.created_at >= cutoff,
        )
    )
    if int(previews or 0) < max(3, settings.control_mutation_attempts * 3):
        return
    await append_control_audit(
        session,
        event="rollout_plan_lifecycle_preview",
        outcome="rejected",
        reason_code="rate_limited",
        evidence={"window_seconds": settings.control_mutation_window_seconds},
        identity=identity,
    )
    await session.commit()
    raise HTTPException(status_code=429, detail="control preview rate limited")


async def _item_evidence(
    session: AsyncSession,
    item: ToolRolloutPlanItemRecord,
    now: datetime,
    settings: Settings,
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    descriptor = await session.scalar(
        select(ToolDescriptorRecord).where(
            ToolDescriptorRecord.tool_id == item.tool_id,
            ToolDescriptorRecord.version == item.descriptor_version,
            ToolDescriptorRecord.descriptor_hash == item.descriptor_hash,
        )
    )
    provider = await session.get(ToolProvider, item.provider_id)
    credential = await session.scalar(
        select(ToolProviderCredential).where(
            ToolProviderCredential.provider_id == item.provider_id
        )
    )
    inventory = await session.scalar(
        select(ToolProviderInventorySnapshot)
        .where(ToolProviderInventorySnapshot.provider_id == item.provider_id)
        .order_by(ToolProviderInventorySnapshot.sequence.desc())
        .limit(1)
    )
    entry = (
        None
        if inventory is None
        else await session.scalar(
            select(ToolProviderInventoryEntry).where(
                ToolProviderInventoryEntry.snapshot_id == inventory.id,
                ToolProviderInventoryEntry.tool_id == item.tool_id,
            )
        )
    )
    heartbeat = await session.scalar(
        select(ToolProviderHeartbeat)
        .where(ToolProviderHeartbeat.provider_id == item.provider_id)
        .order_by(ToolProviderHeartbeat.sequence.desc())
        .limit(1)
    )

    parsed_descriptor: ToolDescriptor | None = None
    if descriptor is None:
        blockers.append("descriptor_missing")
    else:
        parsed_descriptor = ToolDescriptor.model_validate(descriptor.descriptor_json)
        if descriptor.lifecycle != "active":
            blockers.append("descriptor_not_active")
        if descriptor.resource_version != item.expected_descriptor_resource_version:
            blockers.append("descriptor_resource_version_mismatch")
        if item.caller not in parsed_descriptor.allowed_callers:
            blockers.append("caller_forbidden")
        if parsed_descriptor.permission != "public":
            blockers.append("principal_authorization_unavailable")
        if parsed_descriptor.confirmation != "never":
            blockers.append("confirmation_unavailable")

    if provider is None:
        blockers.append("provider_missing")
    else:
        if provider.lifecycle != "active":
            blockers.append("provider_not_active")
        if provider.resource_version != item.expected_provider_resource_version:
            blockers.append("provider_resource_version_mismatch")
        if parsed_descriptor is not None:
            if item.provider_id not in parsed_descriptor.provider_selector.provider_ids:
                blockers.append("provider_not_selected")
            if item.tool_id not in provider.tool_selectors_json:
                blockers.append("provider_tool_selector_mismatch")
            if parsed_descriptor.provider_selector.protocol not in provider.allowed_protocols_json:
                blockers.append("protocol_incompatible")
    if credential is None or credential.lifecycle != "active":
        blockers.append("provider_credential_inactive")
    if inventory is None:
        blockers.append("inventory_missing")
    else:
        if (now - _aware(inventory.received_at)).total_seconds() > settings.provider_inventory_stale_seconds:
            blockers.append("inventory_stale")
    if entry is None:
        blockers.append("inventory_tool_missing")
    elif parsed_descriptor is not None:
        if (
            entry.descriptor_version != item.descriptor_version
            or entry.descriptor_hash != item.descriptor_hash
        ):
            blockers.append("descriptor_mismatch")
        if entry.protocol_version != parsed_descriptor.provider_selector.protocol:
            blockers.append("protocol_incompatible")
        if any(
            entry.budget_enforcement_json.get(name) != "hard"
            for name in parsed_descriptor.required_budget_enforcement
        ):
            blockers.append("budget_unenforceable")
        if not entry.implementation_hash:
            blockers.append("implementation_missing")
    if heartbeat is None:
        blockers.append("heartbeat_missing")
    else:
        if (now - _aware(heartbeat.received_at)).total_seconds() > settings.provider_heartbeat_stale_seconds:
            blockers.append("heartbeat_stale")
        if heartbeat.health != "healthy":
            blockers.append("provider_unhealthy")
        if inventory is not None and heartbeat.inventory_hash != inventory.snapshot_hash:
            blockers.append("inventory_hash_mismatch")

    evidence = {
        "item_id": item.item_id,
        "scope": {
            "tool_id": item.tool_id,
            "descriptor_version": item.descriptor_version,
            "descriptor_hash": item.descriptor_hash,
            "canonical_conversation": item.canonical_conversation,
            "caller": item.caller,
            "provider_id": item.provider_id,
        },
        "expected_versions": {
            "descriptor": item.expected_descriptor_resource_version,
            "provider": item.expected_provider_resource_version,
        },
        "observed_versions": {
            "descriptor": None if descriptor is None else descriptor.resource_version,
            "provider": None if provider is None else provider.resource_version,
        },
        "runtime": {
            "credential_lifecycle": None if credential is None else credential.lifecycle,
            "inventory_hash": None if inventory is None else inventory.snapshot_hash,
            "inventory_received_at": None if inventory is None else _iso(inventory.received_at),
            "heartbeat_health": None if heartbeat is None else heartbeat.health,
            "heartbeat_inventory_hash": None if heartbeat is None else heartbeat.inventory_hash,
            "heartbeat_received_at": None if heartbeat is None else _iso(heartbeat.received_at),
            "implementation_hash": None if entry is None else entry.implementation_hash,
            "budget_enforcement": None if entry is None else entry.budget_enforcement_json,
        },
        "blockers": sorted(set(blockers)),
    }
    return evidence, blockers


async def _build_preview(
    session: AsyncSession,
    payload: RolloutPlanLifecyclePreviewIn,
    settings: Settings,
    *,
    now: datetime,
    for_update: bool,
) -> tuple[ToolRolloutPlanRecord, dict[str, Any], str]:
    statement = select(ToolRolloutPlanRecord).where(
        ToolRolloutPlanRecord.plan_id == payload.plan_id,
        ToolRolloutPlanRecord.version == payload.version,
        ToolRolloutPlanRecord.plan_hash == payload.plan_hash,
    )
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise HTTPException(status_code=404, detail="exact rollout plan was not found")
    items = list(
        (
            await session.scalars(
                select(ToolRolloutPlanItemRecord)
                .where(ToolRolloutPlanItemRecord.plan_record_id == record.id)
                .order_by(ToolRolloutPlanItemRecord.item_id)
            )
        ).all()
    )
    counter = await session.get(ToolRolloutPlanCounter, record.id)
    active_invocations = int(
        await session.scalar(
            select(func.count(ToolInvocation.id)).where(
                ToolInvocation.rollout_plan_id == record.id,
                ToolInvocation.state.in_({"queued", "leased", "running", "cancel_requested"}),
            )
        )
        or 0
    )
    blockers: list[str] = []
    if (record.lifecycle, payload.desired_lifecycle) not in _LEGAL_TRANSITIONS:
        blockers.append("illegal_lifecycle_transition")
    item_evidence: list[dict[str, Any]] = []
    if payload.desired_lifecycle == "active":
        if settings.tool_execution_mode != "canary":
            blockers.append("execution_ceiling_not_canary")
        if settings.tool_global_stop:
            blockers.append("global_stop_enabled")
        if _aware(record.starts_at) > now:
            blockers.append("plan_not_started")
        if _aware(record.expires_at) <= now:
            blockers.append("plan_expired")
        if counter is None:
            blockers.append("counter_missing")
        elif counter.consumed_invocations >= record.max_invocations:
            blockers.append("invocation_limit_exhausted")
        other_active = await session.scalar(
            select(ToolRolloutPlanRecord.id).where(
                ToolRolloutPlanRecord.lifecycle == "active",
                ToolRolloutPlanRecord.id != record.id,
            )
        )
        if other_active is not None:
            blockers.append("another_plan_active")
        for item in items:
            evidence, item_blockers = await _item_evidence(session, item, now, settings)
            item_evidence.append(evidence)
            blockers.extend(f"{item.item_id}:{blocker}" for blocker in item_blockers)
    else:
        item_evidence = [
            {
                "item_id": item.item_id,
                "scope": {
                    "tool_id": item.tool_id,
                    "descriptor_version": item.descriptor_version,
                    "descriptor_hash": item.descriptor_hash,
                    "canonical_conversation": item.canonical_conversation,
                    "caller": item.caller,
                    "provider_id": item.provider_id,
                },
            }
            for item in items
        ]
    before = {
        "lifecycle": record.lifecycle,
        "resource_version": record.resource_version,
        "consumed_invocations": None if counter is None else counter.consumed_invocations,
        "active_invocations": active_invocations,
    }
    after = {
        **before,
        "lifecycle": payload.desired_lifecycle,
        "resource_version": record.resource_version + 1,
    }
    preview = {
        "schema_version": "1.0",
        "target": {
            "type": _TARGET_TYPE,
            "id": _target_id(payload),
            "plan_id": record.plan_id,
            "version": record.version,
            "plan_hash": record.plan_hash,
        },
        "authority": {
            "source_commit": record.source_commit,
            "bundle_hash": record.bundle_hash,
            "reviewer": record.reviewer,
            "mode": record.mode,
            "rollback_mode": record.rollback_mode,
            "starts_at": _iso(record.starts_at),
            "expires_at": _iso(record.expires_at),
            "max_invocations": record.max_invocations,
            "reason": record.reason,
        },
        "execution_ceiling": {
            "mode": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
        },
        "transition": {
            "allowed": not blockers,
            "blockers": sorted(set(blockers)),
            "authority_change": (
                "increase" if payload.desired_lifecycle == "active" else "decrease"
            ),
        },
        "before": before,
        "after": after,
        "items": item_evidence,
    }
    canonical = canonicalize_json_value(preview)
    return record, canonical.value, canonical.sha256


async def create_rollout_plan_lifecycle_preview(
    session: AsyncSession,
    request: Request,
    payload: RolloutPlanLifecyclePreviewIn,
    settings: Settings,
) -> dict[str, Any]:
    identity, now = await _authorize_operator(
        session,
        request,
        settings,
        event="rollout_plan_lifecycle_preview",
        desired_lifecycle=payload.desired_lifecycle,
        require_fresh=False,
    )
    await _enforce_preview_rate(session, identity, now, settings)
    try:
        record, preview, preview_hash = await _build_preview(
            session,
            payload,
            settings,
            now=now,
            for_update=False,
        )
    except HTTPException as exc:
        await append_control_audit(
            session,
            event="rollout_plan_lifecycle_preview",
            outcome="rejected",
            reason_code="preview_target_rejected",
            evidence={"target_id": _target_id(payload), "status_code": exc.status_code},
            identity=identity,
        )
        await session.commit()
        raise
    expires_at = now + timedelta(seconds=settings.control_preview_seconds)
    preview_record = ControlPlanePreview(
        id=new_id(),
        session_id=identity.session_id,
        operator_id=identity.operator_id,
        role=identity.role,
        operation=_OPERATION,
        target_type=_TARGET_TYPE,
        target_id=_target_id(payload),
        request_hash=_request_hash(_preview_request(payload)),
        expected_version=record.resource_version,
        preview_json=preview,
        preview_hash=preview_hash,
        expires_at=expires_at,
    )
    session.add(preview_record)
    await append_control_audit(
        session,
        event="rollout_plan_lifecycle_preview",
        outcome="accepted",
        reason_code="preview_generated",
        evidence={
            "preview_id": preview_record.id,
            "preview_hash": preview_hash,
            "target_id": preview_record.target_id,
            "expected_version": record.resource_version,
            "allowed": preview["transition"]["allowed"],
        },
        identity=identity,
    )
    await session.commit()
    return {
        "schema_version": "1.0",
        "preview_id": preview_record.id,
        "preview_hash": preview_hash,
        "expected_version": record.resource_version,
        "expires_at": expires_at,
        "preview": preview,
    }


async def _existing_mutation(
    session: AsyncSession,
    identity: ControlSessionIdentity,
    idempotency_key: str,
) -> ControlPlaneMutation | None:
    return await session.scalar(
        select(ControlPlaneMutation).where(
            ControlPlaneMutation.operator_id == identity.operator_id,
            ControlPlaneMutation.operation == _OPERATION,
            ControlPlaneMutation.idempotency_key == idempotency_key,
        )
    )


def _stored_result(record: ControlPlaneMutation, *, duplicate: bool) -> tuple[dict, int]:
    return (
        {**record.result_json, "duplicate": duplicate},
        200 if record.outcome == "accepted" else 409,
    )


async def _record_rejection(
    session: AsyncSession,
    *,
    identity: ControlSessionIdentity,
    idempotency_key: str,
    request_hash: str,
    payload: RolloutPlanLifecycleApplyIn,
    reason: str,
    reason_code: str,
    before: dict[str, Any] | None,
) -> tuple[dict, int]:
    mutation_id = new_id()
    result = {
        "schema_version": "1.0",
        "mutation_id": mutation_id,
        "outcome": "rejected",
        "reason_code": reason_code,
        "target_id": _target_id(payload),
        "expected_version": payload.expected_version,
    }
    canonical_result = canonicalize_json_value(result)
    before_hash = None if before is None else canonicalize_json_value(before).sha256
    session.add(
        ControlPlaneMutation(
            id=mutation_id,
            session_id=identity.session_id,
            operator_id=identity.operator_id,
            role=identity.role,
            operation=_OPERATION,
            target_type=_TARGET_TYPE,
            target_id=_target_id(payload),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            expected_version=payload.expected_version,
            preview_hash=payload.preview_hash,
            before_hash=before_hash,
            after_hash=None,
            outcome="rejected",
            reason_code=reason_code,
            reason=reason,
            result_json=canonical_result.value,
            result_hash=canonical_result.sha256,
        )
    )
    await append_control_audit(
        session,
        event=_OPERATION,
        outcome="rejected",
        reason_code=reason_code,
        evidence={
            "mutation_id": mutation_id,
            "target_id": _target_id(payload),
            "request_hash": request_hash,
            "preview_hash": payload.preview_hash,
            "before_hash": before_hash,
        },
        identity=identity,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _existing_mutation(session, identity, idempotency_key)
        if existing is not None and existing.request_hash == request_hash:
            return _stored_result(existing, duplicate=True)
        if existing is not None:
            await append_control_audit(
                session,
                event=_OPERATION,
                outcome="rejected",
                reason_code="idempotency_conflict",
                evidence={
                    "target_id": _target_id(payload),
                    "stored_request_hash": existing.request_hash,
                    "request_hash": request_hash,
                },
                identity=identity,
            )
            await session.commit()
        raise HTTPException(status_code=409, detail="control Idempotency-Key conflicts")
    return {**canonical_result.value, "duplicate": False}, 409


async def apply_rollout_plan_lifecycle_mutation(
    session: AsyncSession,
    request: Request,
    payload: RolloutPlanLifecycleApplyIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[dict, int]:
    identity, now = await _authorize_operator(
        session,
        request,
        settings,
        event=_OPERATION,
        desired_lifecycle=payload.desired_lifecycle,
        require_fresh=True,
    )
    idempotency_key = _idempotency_key(idempotency_key)
    reason = _exact_reason(payload.reason)
    request_hash = _request_hash(payload.model_dump(mode="json"))
    existing = await _existing_mutation(session, identity, idempotency_key)
    if existing is not None:
        if existing.request_hash == request_hash:
            return _stored_result(existing, duplicate=True)
        await _enforce_mutation_rate(session, identity, now, settings)
        await append_control_audit(
            session,
            event=_OPERATION,
            outcome="rejected",
            reason_code="idempotency_conflict",
            evidence={
                "target_id": _target_id(payload),
                "stored_request_hash": existing.request_hash,
                "request_hash": request_hash,
            },
            identity=identity,
        )
        await session.commit()
        raise HTTPException(status_code=409, detail="control Idempotency-Key conflicts")
    await _enforce_mutation_rate(session, identity, now, settings)

    preview_record = await session.scalar(
        select(ControlPlanePreview).where(ControlPlanePreview.id == payload.preview_id)
    )
    if (
        preview_record is None
        or preview_record.session_id != identity.session_id
        or preview_record.operator_id != identity.operator_id
        or preview_record.operation != _OPERATION
        or preview_record.target_id != _target_id(payload)
        or preview_record.preview_hash != payload.preview_hash
        or preview_record.expected_version != payload.expected_version
        or preview_record.request_hash != _request_hash(_preview_request(payload))
    ):
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="preview_invalid",
            before=None,
        )
    if _aware(preview_record.expires_at) <= now:
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="preview_expired",
            before=preview_record.preview_json.get("before"),
        )

    record, current_preview, current_preview_hash = await _build_preview(
        session,
        payload,
        settings,
        now=now,
        for_update=True,
    )
    if record.resource_version != payload.expected_version:
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="resource_version_conflict",
            before=current_preview["before"],
        )
    runtime_drift_tolerated = (
        payload.desired_lifecycle == "paused"
        and current_preview_hash != preview_record.preview_hash
    )
    if current_preview_hash != preview_record.preview_hash and not runtime_drift_tolerated:
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="preview_stale",
            before=current_preview["before"],
        )
    if not current_preview["transition"]["allowed"]:
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="preview_not_applicable",
            before=current_preview["before"],
        )

    next_version = record.resource_version + 1
    mutation_id = new_id()
    before_hash = canonicalize_json_value(current_preview["before"]).sha256
    after_hash = canonicalize_json_value(current_preview["after"]).sha256
    result = {
        "schema_version": "1.0",
        "mutation_id": mutation_id,
        "outcome": "accepted",
        "reason_code": "lifecycle_changed",
        "target_id": _target_id(payload),
        "previous_lifecycle": record.lifecycle,
        "lifecycle": payload.desired_lifecycle,
        "resource_version": next_version,
        "preview_hash": preview_record.preview_hash,
        "recomputed_preview_hash": current_preview_hash,
        "runtime_drift_tolerated": runtime_drift_tolerated,
    }
    canonical_result = canonicalize_json_value(result)
    try:
        session.add(
            ToolRolloutPlanLifecycleEvent(
                id=new_id(),
                plan_record_id=record.id,
                sequence=next_version,
                previous_lifecycle=record.lifecycle,
                lifecycle=payload.desired_lifecycle,
                actor=identity.operator_id,
                reason=reason,
            )
        )
        await session.flush()
        changed = await session.execute(
            update(ToolRolloutPlanRecord)
            .where(
                ToolRolloutPlanRecord.id == record.id,
                ToolRolloutPlanRecord.lifecycle == record.lifecycle,
                ToolRolloutPlanRecord.resource_version == record.resource_version,
            )
            .values(
                lifecycle=payload.desired_lifecycle,
                resource_version=next_version,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            await session.rollback()
            return await _record_rejection(
                session,
                identity=identity,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                payload=payload,
                reason=reason,
                reason_code="resource_version_conflict",
                before=current_preview["before"],
            )
        session.add(
            ControlPlaneMutation(
                id=mutation_id,
                session_id=identity.session_id,
                operator_id=identity.operator_id,
                role=identity.role,
                operation=_OPERATION,
                target_type=_TARGET_TYPE,
                target_id=_target_id(payload),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                expected_version=payload.expected_version,
                preview_hash=preview_record.preview_hash,
                before_hash=before_hash,
                after_hash=after_hash,
                outcome="accepted",
                reason_code="lifecycle_changed",
                reason=reason,
                result_json=canonical_result.value,
                result_hash=canonical_result.sha256,
            )
        )
        await append_control_audit(
            session,
            event=_OPERATION,
            outcome="accepted",
            reason_code="lifecycle_changed",
            evidence={
                "mutation_id": mutation_id,
                "target_id": _target_id(payload),
                "request_hash": request_hash,
                "preview_hash": preview_record.preview_hash,
                "recomputed_preview_hash": current_preview_hash,
                "runtime_drift_tolerated": runtime_drift_tolerated,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "resource_version": next_version,
            },
            identity=identity,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await _existing_mutation(session, identity, idempotency_key)
        if duplicate is not None and duplicate.request_hash == request_hash:
            return _stored_result(duplicate, duplicate=True)
        return await _record_rejection(
            session,
            identity=identity,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            payload=payload,
            reason=reason,
            reason_code="resource_version_conflict",
            before=current_preview["before"],
        )
    return {**canonical_result.value, "duplicate": False}, 200
