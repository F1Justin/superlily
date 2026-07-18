"""M1 descriptor lifecycle preview、CAS、幂等和只追加审计。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Literal

from fastapi import HTTPException, Request, status
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
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    new_id,
)
from .settings import Settings
from .tool_registry_service import tool_registry_view


_OPERATION = "descriptor_lifecycle_apply"
_TARGET_TYPE = "tool_descriptor"
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$")
_LEGAL_TRANSITIONS = {
    ("reviewed", "active"),
    ("active", "suspended"),
    ("suspended", "active"),
}


class _ControlMutationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DescriptorLifecyclePreviewIn(_ControlMutationModel):
    schema_version: Literal["1.0"] = "1.0"
    tool_id: str = Field(
        min_length=3,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
    )
    descriptor_version: str = Field(
        min_length=5,
        max_length=64,
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"
        ),
    )
    descriptor_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_lifecycle: Literal["active", "suspended"]


class DescriptorLifecycleApplyIn(DescriptorLifecyclePreviewIn):
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


def _target_id(payload: DescriptorLifecyclePreviewIn) -> str:
    return f"{payload.tool_id}@{payload.descriptor_version}#{payload.descriptor_hash}"


def _preview_request(payload: DescriptorLifecyclePreviewIn) -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": payload.tool_id,
        "descriptor_version": payload.descriptor_version,
        "descriptor_hash": payload.descriptor_hash,
        "desired_lifecycle": payload.desired_lifecycle,
    }


def _request_hash(value: dict) -> str:
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


async def _authorize_reviewer(
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
    if identity.role != "reviewer":
        await append_control_audit(
            session,
            event=event,
            outcome="rejected",
            reason_code="role_forbidden",
            evidence={"required_role": "reviewer"},
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
            ControlPlanePreview.created_at >= cutoff,
        )
    )
    limit = max(3, settings.control_mutation_attempts * 3)
    if int(previews or 0) < limit:
        return
    await append_control_audit(
        session,
        event="descriptor_lifecycle_preview",
        outcome="rejected",
        reason_code="rate_limited",
        evidence={"window_seconds": settings.control_mutation_window_seconds},
        identity=identity,
    )
    await session.commit()
    raise HTTPException(status_code=429, detail="control preview rate limited")


async def _build_preview(
    session: AsyncSession,
    payload: DescriptorLifecyclePreviewIn,
    settings: Settings,
    *,
    for_update: bool,
) -> tuple[ToolDescriptorRecord, dict, str]:
    statement = select(ToolDescriptorRecord).where(
        ToolDescriptorRecord.tool_id == payload.tool_id,
        ToolDescriptorRecord.version == payload.descriptor_version,
        ToolDescriptorRecord.descriptor_hash == payload.descriptor_hash,
    )
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    if record is None:
        raise HTTPException(status_code=404, detail="exact tool descriptor was not found")

    registry = await tool_registry_view(session, settings, tool_id=payload.tool_id)
    tool = next(
        (
            item
            for item in registry["tools"]
            if item["version"] == payload.descriptor_version
            and item["desired"]["descriptor_hash"] == payload.descriptor_hash
        ),
        None,
    )
    if tool is None:
        raise HTTPException(status_code=409, detail="descriptor registry view is inconsistent")
    descriptor = ToolDescriptor.model_validate(record.descriptor_json)
    transition = (record.lifecycle, payload.desired_lifecycle)
    blockers: list[str] = []
    if transition not in _LEGAL_TRANSITIONS:
        blockers.append("transition_not_allowed")
    if payload.desired_lifecycle == "active":
        if settings.tool_execution_mode == "off":
            blockers.append("execution_off")
        if settings.tool_global_stop:
            blockers.append("global_stop")
        if not any(item["runtime_eligible"] for item in tool["reported"]):
            runtime_reasons = sorted(
                {
                    reason
                    for item in tool["reported"]
                    for reason in item["reasons"]
                }
                or {"provider_missing"}
            )
            blockers.extend(runtime_reasons)

    before_reasons = list(tool["effective"]["reasons"])
    after_reasons = set(before_reasons)
    if payload.desired_lifecycle == "active":
        after_reasons.discard("inactive_descriptor")
        after_reasons.discard("tool_suspended")
    else:
        after_reasons.update({"inactive_descriptor", "tool_suspended"})
    after_reasons_ordered = sorted(after_reasons)
    before = {
        "resource_version": record.resource_version,
        "lifecycle": record.lifecycle,
        "effective": {
            "eligible": tool["effective"]["eligible"],
            "execution_mode": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
            "reasons": before_reasons,
        },
    }
    after = {
        "resource_version": record.resource_version + 1,
        "lifecycle": payload.desired_lifecycle,
        "effective": {
            "eligible": not after_reasons_ordered,
            "execution_mode": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
            "reasons": after_reasons_ordered,
        },
    }
    preview = {
        "schema_version": "1.0",
        "operation": _OPERATION,
        "target": {
            "type": _TARGET_TYPE,
            "id": _target_id(payload),
            "tool_id": record.tool_id,
            "descriptor_version": record.version,
            "descriptor_hash": record.descriptor_hash,
        },
        "authority": {
            "source_commit": record.source_commit,
            "bundle_hash": record.bundle_hash,
            "reviewer": record.reviewer,
            "permission": descriptor.permission,
            "side_effect": descriptor.side_effect,
            "confirmation": descriptor.confirmation,
            "data_classification": descriptor.data_classification,
            "allowed_callers": descriptor.allowed_callers,
            "natural_language": descriptor.natural_language,
            "execution_permissions": descriptor.execution_permissions.model_dump(mode="json"),
            "required_budget_enforcement": descriptor.required_budget_enforcement,
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
        "runtime": tool["reported"],
    }
    canonical = canonicalize_json_value(preview)
    return record, canonical.value, canonical.sha256


async def create_descriptor_lifecycle_preview(
    session: AsyncSession,
    request: Request,
    payload: DescriptorLifecyclePreviewIn,
    settings: Settings,
) -> dict:
    identity, now = await _authorize_reviewer(
        session,
        request,
        settings,
        event="descriptor_lifecycle_preview",
        require_fresh=False,
    )
    await _enforce_preview_rate(session, identity, now, settings)
    try:
        record, preview, preview_hash = await _build_preview(
            session,
            payload,
            settings,
            for_update=False,
        )
    except HTTPException as exc:
        await append_control_audit(
            session,
            event="descriptor_lifecycle_preview",
            outcome="rejected",
            reason_code="preview_target_rejected",
            evidence={
                "target_id": _target_id(payload),
                "status_code": exc.status_code,
            },
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
        target_id=_target_id(payload),
        request_hash=request_hash,
        expected_version=record.resource_version,
        preview_json=preview,
        preview_hash=preview_hash,
        expires_at=expires_at,
    )
    session.add(preview_record)
    await append_control_audit(
        session,
        event="descriptor_lifecycle_preview",
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
    status_code = 200 if record.outcome == "accepted" else 409
    return {**record.result_json, "duplicate": duplicate}, status_code


async def _record_rejection(
    session: AsyncSession,
    *,
    identity: ControlSessionIdentity,
    idempotency_key: str,
    request_hash: str,
    payload: DescriptorLifecycleApplyIn,
    reason: str,
    reason_code: str,
    before: dict | None,
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


async def apply_descriptor_lifecycle_mutation(
    session: AsyncSession,
    request: Request,
    payload: DescriptorLifecycleApplyIn,
    idempotency_key: str,
    settings: Settings,
) -> tuple[dict, int]:
    identity, now = await _authorize_reviewer(
        session,
        request,
        settings,
        event=_OPERATION,
        require_fresh=True,
    )
    idempotency_key = _idempotency_key(idempotency_key)
    reason = _exact_reason(payload.reason)
    request_value = payload.model_dump(mode="json")
    request_hash = _request_hash(request_value)
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

    next_version = record.resource_version + 1
    lifecycle_event = ToolDescriptorLifecycleEvent(
        id=new_id(),
        descriptor_id=record.id,
        sequence=next_version,
        previous_lifecycle=record.lifecycle,
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
        "reason_code": "lifecycle_changed",
        "target_id": _target_id(payload),
        "previous_lifecycle": record.lifecycle,
        "lifecycle": payload.desired_lifecycle,
        "resource_version": next_version,
        "preview_hash": current_preview_hash,
    }
    canonical_result = canonicalize_json_value(result)
    try:
        session.add(lifecycle_event)
        await session.flush()
        changed = await session.execute(
            update(ToolDescriptorRecord)
            .where(
                ToolDescriptorRecord.id == record.id,
                ToolDescriptorRecord.lifecycle == record.lifecycle,
                ToolDescriptorRecord.resource_version == record.resource_version,
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
                target_id=_target_id(payload),
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                expected_version=payload.expected_version,
                preview_hash=current_preview_hash,
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
