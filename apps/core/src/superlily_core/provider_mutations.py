"""M2 Provider quarantine preview、CAS、幂等与只追加证据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import canonicalize_json_value

from .control_plane import (
    ControlSessionIdentity,
    append_control_audit,
    authenticate_control_session,
)
from .models import (
    ControlPlaneAuditEvent,
    ControlPlaneMutation,
    ControlPlanePreview,
    ToolAttempt,
    ToolProvider,
    ToolProviderCredential,
    ToolProviderHeartbeat,
    ToolProviderInventoryEntry,
    ToolProviderInventorySnapshot,
    ToolProviderLifecycleEvent,
    new_id,
)
from .settings import Settings
from .tool_registry_service import tool_registry_view


_OPERATION = "provider_lifecycle_apply"
_TARGET_TYPE = "tool_provider"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_LEGAL_TRANSITIONS = {
    ("active", "quarantined"),
    ("quarantined", "active"),
}


class _ProviderMutationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProviderLifecyclePreviewIn(_ProviderMutationModel):
    schema_version: Literal["1.0"] = "1.0"
    provider_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    desired_lifecycle: Literal["active", "quarantined"]


class ProviderLifecycleApplyIn(ProviderLifecyclePreviewIn):
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


def _age_seconds(value: datetime, now: datetime) -> int:
    return max(0, int((now - _aware(value)).total_seconds()))


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


def _preview_request(payload: ProviderLifecyclePreviewIn) -> dict[str, str]:
    return {
        "schema_version": "1.0",
        "provider_id": payload.provider_id,
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


async def _authorize_security_admin(
    session: AsyncSession,
    request: Request,
    settings: Settings,
    *,
    event: str,
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
    if identity.role != "security_admin":
        await append_control_audit(
            session,
            event=event,
            outcome="rejected",
            reason_code="role_forbidden",
            evidence={"required_role": "security_admin"},
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
    limit = max(3, settings.control_mutation_attempts * 3)
    if int(previews or 0) < limit:
        return
    await append_control_audit(
        session,
        event="provider_lifecycle_preview",
        outcome="rejected",
        reason_code="rate_limited",
        evidence={"window_seconds": settings.control_mutation_window_seconds},
        identity=identity,
    )
    await session.commit()
    raise HTTPException(status_code=429, detail="control preview rate limited")


def _simulated_tool_impacts(
    registry: dict[str, Any],
    provider_id: str,
    desired_lifecycle: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    impacts: list[dict[str, Any]] = []
    for tool in registry["tools"]:
        reported = [dict(item) for item in tool["reported"]]
        if not any(item["provider_id"] == provider_id for item in reported):
            continue
        after_reason_sets: list[set[str]] = []
        provider_before: dict[str, Any] | None = None
        provider_after: dict[str, Any] | None = None
        for item in reported:
            reasons = set(item["reasons"])
            if item["provider_id"] == provider_id:
                provider_before = {
                    "runtime_eligible": item["runtime_eligible"],
                    "reasons": list(item["reasons"]),
                }
                if desired_lifecycle == "quarantined":
                    reasons.add("provider_quarantined")
                else:
                    reasons.discard("provider_quarantined")
                provider_after = {
                    "runtime_eligible": not reasons,
                    "reasons": sorted(reasons),
                }
            after_reason_sets.append(reasons)

        desired = tool["desired"]
        base_reasons: set[str] = set()
        if desired["review_status"] != "reviewed":
            base_reasons.add("not_reviewed")
        if desired["lifecycle"] != "active":
            base_reasons.add("inactive_descriptor")
        if desired["lifecycle"] == "suspended":
            base_reasons.add("tool_suspended")
        if after_reason_sets and not any(not reasons for reasons in after_reason_sets):
            for reasons in after_reason_sets:
                base_reasons.update(reasons)
        elif not after_reason_sets:
            base_reasons.add("provider_missing")
        if settings.tool_execution_mode == "off":
            base_reasons.add("execution_off")
        if settings.tool_global_stop:
            base_reasons.add("global_stop")
        after_effective_reasons = sorted(base_reasons)
        impacts.append(
            {
                "tool_id": tool["tool_id"],
                "descriptor_version": tool["version"],
                "descriptor_hash": desired["descriptor_hash"],
                "provider_before": provider_before,
                "provider_after": provider_after,
                "effective_before": tool["effective"],
                "effective_after": {
                    "eligible": not after_effective_reasons,
                    "reasons": after_effective_reasons,
                },
            }
        )
    return impacts


async def _runtime_snapshot(
    session: AsyncSession,
    provider: ToolProvider,
    now: datetime,
    settings: Settings,
) -> tuple[dict[str, Any], list[str]]:
    credential = await session.get(ToolProviderCredential, provider.id)
    inventory = await session.scalar(
        select(ToolProviderInventorySnapshot)
        .where(ToolProviderInventorySnapshot.provider_id == provider.id)
        .order_by(ToolProviderInventorySnapshot.sequence.desc())
        .limit(1)
    )
    entries = []
    if inventory is not None:
        entries = list(
            (
                await session.scalars(
                    select(ToolProviderInventoryEntry)
                    .where(ToolProviderInventoryEntry.snapshot_id == inventory.id)
                    .order_by(ToolProviderInventoryEntry.tool_id)
                )
            ).all()
        )
    heartbeat = await session.scalar(
        select(ToolProviderHeartbeat)
        .where(ToolProviderHeartbeat.provider_id == provider.id)
        .order_by(ToolProviderHeartbeat.sequence.desc())
        .limit(1)
    )
    inventory_age = None if inventory is None else _age_seconds(inventory.received_at, now)
    heartbeat_age = None if heartbeat is None else _age_seconds(heartbeat.received_at, now)
    inventory_fresh = (
        inventory_age is not None
        and inventory_age <= settings.provider_inventory_stale_seconds
    )
    heartbeat_fresh = (
        heartbeat_age is not None
        and heartbeat_age <= settings.provider_heartbeat_stale_seconds
    )

    blockers: list[str] = []
    if credential is None or credential.lifecycle != "active":
        blockers.append("credential_inactive")
    if inventory is None:
        blockers.append("inventory_missing")
    elif not inventory_fresh:
        blockers.append("inventory_stale")
    if heartbeat is None:
        blockers.append("heartbeat_missing")
    else:
        if not heartbeat_fresh:
            blockers.append("heartbeat_stale")
        if heartbeat.health != "healthy":
            blockers.append("provider_unhealthy")
    if inventory is not None and inventory.protocol_version not in provider.allowed_protocols_json:
        blockers.append("protocol_incompatible")
    if inventory is not None and heartbeat is not None:
        if heartbeat.inventory_hash != inventory.snapshot_hash:
            blockers.append("inventory_heartbeat_mismatch")
    if not entries:
        blockers.append("inventory_empty")
    if any(not entry.implementation_hash for entry in entries):
        blockers.append("implementation_hash_missing")

    snapshot = {
        "credential": {
            "configured": credential is not None,
            "lifecycle": None if credential is None else credential.lifecycle,
        },
        "inventory": None
        if inventory is None
        else {
            "snapshot_hash": inventory.snapshot_hash,
            "protocol_version": inventory.protocol_version,
            "received_at": _iso(inventory.received_at),
            "fresh": inventory_fresh,
            "entries": [
                {
                    "tool_id": entry.tool_id,
                    "descriptor_hash": entry.descriptor_hash,
                    "implementation_hash": entry.implementation_hash,
                    "protocol_version": entry.protocol_version,
                    "budget_enforcement": entry.budget_enforcement_json,
                }
                for entry in entries
            ],
        },
        "heartbeat": None
        if heartbeat is None
        else {
            "inventory_hash": heartbeat.inventory_hash,
            "health": heartbeat.health,
            "current_concurrency": heartbeat.current_concurrency,
            "max_concurrency": heartbeat.max_concurrency,
            "oldest_work_age_ms": heartbeat.oldest_work_age_ms,
            "received_at": _iso(heartbeat.received_at),
            "fresh": heartbeat_fresh,
        },
    }
    return snapshot, sorted(set(blockers))


async def _build_preview(
    session: AsyncSession,
    payload: ProviderLifecyclePreviewIn,
    settings: Settings,
    *,
    now: datetime,
    for_update: bool,
) -> tuple[ToolProvider, dict[str, Any], str]:
    statement = select(ToolProvider).where(ToolProvider.id == payload.provider_id)
    if for_update:
        statement = statement.with_for_update()
    provider = await session.scalar(statement)
    if provider is None:
        raise HTTPException(status_code=404, detail="exact tool provider was not found")

    transition = (provider.lifecycle, payload.desired_lifecycle)
    runtime, restore_blockers = await _runtime_snapshot(session, provider, now, settings)
    blockers: list[str] = []
    if transition not in _LEGAL_TRANSITIONS:
        blockers.append("transition_not_allowed")
    if payload.desired_lifecycle == "active":
        blockers.extend(restore_blockers)

    registry = await tool_registry_view(session, settings)
    active_attempts = await session.scalar(
        select(func.count(ToolAttempt.id)).where(
            ToolAttempt.provider_id == provider.id,
            ToolAttempt.state.in_({"leased", "running"}),
        )
    )
    preview = {
        "schema_version": "1.0",
        "operation": _OPERATION,
        "target": {
            "type": _TARGET_TYPE,
            "id": provider.id,
            "provider_id": provider.id,
        },
        "authority": {
            "owner": provider.owner,
            "allowed_protocols": provider.allowed_protocols_json,
            "tool_selectors": provider.tool_selectors_json,
        },
        "transition": {
            "allowed": not blockers,
            "blockers": sorted(set(blockers)),
            "authority_change": (
                "increase" if payload.desired_lifecycle == "active" else "decrease"
            ),
        },
        "before": {
            "resource_version": provider.resource_version,
            "lifecycle": provider.lifecycle,
            "accepts_new_leases": provider.lifecycle == "active",
        },
        "after": {
            "resource_version": provider.resource_version + 1,
            "lifecycle": payload.desired_lifecycle,
            "accepts_new_leases": payload.desired_lifecycle == "active",
        },
        "runtime": runtime,
        "active_attempts": int(active_attempts or 0),
        "affected_tools": _simulated_tool_impacts(
            registry,
            provider.id,
            payload.desired_lifecycle,
            settings,
        ),
    }
    canonical = canonicalize_json_value(preview)
    return provider, canonical.value, canonical.sha256


async def create_provider_lifecycle_preview(
    session: AsyncSession,
    request: Request,
    payload: ProviderLifecyclePreviewIn,
    settings: Settings,
) -> dict[str, Any]:
    identity, now = await _authorize_security_admin(
        session,
        request,
        settings,
        event="provider_lifecycle_preview",
        require_fresh=False,
    )
    await _enforce_preview_rate(session, identity, now, settings)
    try:
        provider, preview, preview_hash = await _build_preview(
            session,
            payload,
            settings,
            now=now,
            for_update=False,
        )
    except HTTPException as exc:
        await append_control_audit(
            session,
            event="provider_lifecycle_preview",
            outcome="rejected",
            reason_code="preview_target_rejected",
            evidence={"target_id": payload.provider_id, "status_code": exc.status_code},
            identity=identity,
        )
        await session.commit()
        raise
    request_hash = _request_hash(_preview_request(payload))
    expires_at = now + timedelta(seconds=settings.control_preview_seconds)
    preview_record = ControlPlanePreview(
        id=new_id(),
        session_id=identity.session_id,
        operator_id=identity.operator_id,
        role=identity.role,
        operation=_OPERATION,
        target_type=_TARGET_TYPE,
        target_id=provider.id,
        request_hash=request_hash,
        expected_version=provider.resource_version,
        preview_json=preview,
        preview_hash=preview_hash,
        expires_at=expires_at,
    )
    session.add(preview_record)
    await append_control_audit(
        session,
        event="provider_lifecycle_preview",
        outcome="accepted",
        reason_code="preview_generated",
        evidence={
            "preview_id": preview_record.id,
            "preview_hash": preview_hash,
            "target_id": provider.id,
            "expected_version": provider.resource_version,
            "allowed": preview["transition"]["allowed"],
        },
        identity=identity,
    )
    await session.commit()
    return {
        "schema_version": "1.0",
        "preview_id": preview_record.id,
        "preview_hash": preview_hash,
        "expected_version": provider.resource_version,
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
    status_code = 200 if record.outcome == "accepted" else 409
    return {**record.result_json, "duplicate": duplicate}, status_code


async def _record_rejection(
    session: AsyncSession,
    *,
    identity: ControlSessionIdentity,
    idempotency_key: str,
    request_hash: str,
    payload: ProviderLifecycleApplyIn,
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
        "target_id": payload.provider_id,
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
            target_id=payload.provider_id,
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
            "target_id": payload.provider_id,
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
                    "target_id": payload.provider_id,
                    "stored_request_hash": existing.request_hash,
                    "request_hash": request_hash,
                },
                identity=identity,
            )
            await session.commit()
        raise HTTPException(status_code=409, detail="control Idempotency-Key conflicts")
    return {**canonical_result.value, "duplicate": False}, 409


async def apply_provider_lifecycle_mutation(
    session: AsyncSession,
    request: Request,
    payload: ProviderLifecycleApplyIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[dict[str, Any], int]:
    identity, now = await _authorize_security_admin(
        session,
        request,
        settings,
        event=_OPERATION,
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
                "target_id": payload.provider_id,
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
        or preview_record.target_id != payload.provider_id
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

    provider, current_preview, current_preview_hash = await _build_preview(
        session,
        payload,
        settings,
        now=now,
        for_update=True,
    )
    if provider.resource_version != payload.expected_version:
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
    if current_preview_hash != preview_record.preview_hash:
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

    next_version = provider.resource_version + 1
    lifecycle_event = ToolProviderLifecycleEvent(
        id=new_id(),
        provider_id=provider.id,
        sequence=next_version,
        previous_lifecycle=provider.lifecycle,
        lifecycle=payload.desired_lifecycle,
        actor=identity.operator_id,
        reason=reason,
    )
    mutation_id = new_id()
    before_hash = canonicalize_json_value(current_preview["before"]).sha256
    after_hash = canonicalize_json_value(current_preview["after"]).sha256
    result = {
        "schema_version": "1.0",
        "mutation_id": mutation_id,
        "outcome": "accepted",
        "reason_code": "provider_lifecycle_changed",
        "target_id": provider.id,
        "previous_lifecycle": provider.lifecycle,
        "lifecycle": payload.desired_lifecycle,
        "resource_version": next_version,
        "preview_hash": current_preview_hash,
    }
    canonical_result = canonicalize_json_value(result)
    try:
        session.add(lifecycle_event)
        await session.flush()
        changed = await session.execute(
            update(ToolProvider)
            .where(
                ToolProvider.id == provider.id,
                ToolProvider.lifecycle == provider.lifecycle,
                ToolProvider.resource_version == provider.resource_version,
            )
            .values(
                lifecycle=payload.desired_lifecycle,
                resource_version=next_version,
            )
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
                target_id=provider.id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                expected_version=payload.expected_version,
                preview_hash=current_preview_hash,
                before_hash=before_hash,
                after_hash=after_hash,
                outcome="accepted",
                reason_code="provider_lifecycle_changed",
                reason=reason,
                result_json=canonical_result.value,
                result_hash=canonical_result.sha256,
            )
        )
        await append_control_audit(
            session,
            event=_OPERATION,
            outcome="accepted",
            reason_code="provider_lifecycle_changed",
            evidence={
                "mutation_id": mutation_id,
                "target_id": provider.id,
                "request_hash": request_hash,
                "preview_hash": current_preview_hash,
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
