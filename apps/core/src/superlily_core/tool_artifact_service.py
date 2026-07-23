"""Provider artifact 的 reserve、upload、finalize、引用与清理协议。"""

from __future__ import annotations

from collections.abc import AsyncIterable
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    ToolArtifactFinalizeIn,
    ToolArtifactReference,
    ToolArtifactReservationOut,
    ToolArtifactReserveIn,
    ToolArtifactUploadOut,
    ToolDescriptor,
    ToolUsage,
    artifact_upload_secret_hash,
    canonicalize_json_value,
)

from .artifact_store import ArtifactStore, ArtifactStoreError
from .models import (
    RenderArtifactRecord,
    ToolArtifact,
    ToolArtifactEvent,
    ToolAttempt,
    ToolInvocation,
    new_id,
)
from .settings import Settings
from .tool_execution_service import (
    _append_attempt_event,
    _locked_attempt_and_invocation,
)
from .tool_invocation_service import database_now


SUPPORTED_ARTIFACT_MIMES = frozenset({"image/png"})
_TERMINAL_ATTEMPT_STATES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "lease_expired",
        "unknown_completion",
    }
)


class ArtifactProtocolError(RuntimeError):
    """Provider 的 artifact 结果不能作为成功完成被接受。"""

    def __init__(self, code: str, safe_detail: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail
        self.evidence = evidence or {}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool artifact not found")


def _snapshot(value: Any) -> tuple[Any, str]:
    canonical = canonicalize_json_value(value)
    return canonical.value, canonical.sha256


def _store(settings: Settings) -> ArtifactStore:
    if not settings.artifact_enabled:
        raise _conflict("artifact storage is disabled")
    return ArtifactStore(settings.artifact_root)


def _reservation_secret(artifact: ToolArtifact, settings: Settings) -> str:
    material = canonicalize_json_value(
        {
            "schema_version": "1.0",
            "artifact_id": artifact.id,
            "invocation_id": artifact.invocation_id,
            "attempt_id": artifact.attempt_id,
            "provider_id": artifact.provider_id,
            "fencing_token": artifact.fencing_token,
            "idempotency_key": artifact.idempotency_key,
            "reservation_request_hash": artifact.reservation_request_hash,
        }
    ).canonical_bytes
    digest = hmac.new(
        settings.artifact_secret_pepper.encode("utf-8"),
        material,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _reservation_out(artifact: ToolArtifact, settings: Settings) -> ToolArtifactReservationOut:
    secret = _reservation_secret(artifact, settings)
    if not secrets.compare_digest(
        artifact.upload_secret_hash,
        artifact_upload_secret_hash(secret),
    ):
        raise RuntimeError("stored artifact upload secret hash is inconsistent")
    return ToolArtifactReservationOut(
        artifact_id=artifact.id,
        invocation_id=artifact.invocation_id,
        attempt_id=artifact.attempt_id,
        fencing_token=artifact.fencing_token,
        upload_secret=secret,
        mime_type=artifact.mime_type,
        max_bytes=artifact.max_bytes,
        max_width_pixels=artifact.max_width_pixels,
        max_height_pixels=artifact.max_height_pixels,
        expires_at=_aware(artifact.expires_at),
    )


async def _append_artifact_event(
    session: AsyncSession,
    artifact: ToolArtifact,
    *,
    sequence: int,
    event: str,
    previous_state: str | None,
    state: str,
    actor_type: str,
    actor_id: str,
    reason_code: str,
    evidence: dict[str, Any],
    effective_at: datetime,
) -> None:
    value, digest = _snapshot(evidence)
    session.add(
        ToolArtifactEvent(
            artifact_id=artifact.id,
            sequence=sequence,
            event=event,
            previous_state=previous_state,
            state=state,
            actor_type=actor_type,
            actor_id=actor_id,
            provider_id=artifact.provider_id,
            fencing_token=artifact.fencing_token,
            reason_code=reason_code,
            evidence_json=value,
            evidence_hash=digest,
            effective_at=effective_at,
            created_at=effective_at,
        )
    )


async def _transition_artifact(
    session: AsyncSession,
    artifact: ToolArtifact,
    *,
    event: str,
    state: str,
    actor_type: str,
    actor_id: str,
    reason_code: str,
    evidence: dict[str, Any],
    effective_at: datetime,
    values: dict[str, Any] | None = None,
) -> ToolArtifact:
    previous_state = artifact.state
    next_version = artifact.resource_version + 1
    await _append_artifact_event(
        session,
        artifact,
        sequence=next_version,
        event=event,
        previous_state=previous_state,
        state=state,
        actor_type=actor_type,
        actor_id=actor_id,
        reason_code=reason_code,
        evidence=evidence,
        effective_at=effective_at,
    )
    await session.flush()
    update_values: dict[str, Any] = {
        "state": state,
        "resource_version": next_version,
        "updated_at": effective_at,
        **(values or {}),
    }
    if event == "finalize":
        update_values["finalized_at"] = effective_at
    elif event == "reject":
        update_values["rejected_at"] = effective_at
    elif event == "expire":
        update_values["expired_at"] = effective_at
    if event == "reference":
        update_values["referenced_at"] = effective_at
    if event == "cleanup":
        update_values["content_deleted_at"] = effective_at
    result = await session.execute(
        update(ToolArtifact)
        .where(
            ToolArtifact.id == artifact.id,
            ToolArtifact.state == previous_state,
            ToolArtifact.resource_version == artifact.resource_version,
        )
        .values(**update_values)
    )
    if result.rowcount != 1:
        raise _conflict("artifact state changed concurrently")
    refreshed = await session.get(ToolArtifact, artifact.id, populate_existing=True)
    assert refreshed is not None
    return refreshed


async def reserve_tool_artifact(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolArtifactReserveIn,
    provider_id: str,
    idempotency_key: str,
    settings: Settings,
) -> tuple[ToolArtifactReservationOut, bool]:
    store = _store(settings)
    await store.initialize()
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="artifact_reserve",
        allowed_attempt_states={"running"},
        allowed_invocation_states={"running"},
    )
    request_value, request_hash = _snapshot(
        {
            "schema_version": "1.0",
            "invocation_id": invocation_id,
            "provider_id": provider_id,
            "idempotency_key": idempotency_key,
            "request": payload.model_dump(mode="json", exclude={"lease_secret"}),
        }
    )
    existing = await session.scalar(
        select(ToolArtifact).where(
            ToolArtifact.provider_id == provider_id,
            ToolArtifact.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.reservation_request_hash != request_hash
            or existing.invocation_id != invocation.id
            or existing.attempt_id != attempt.id
            or existing.fencing_token != attempt.fencing_token
        ):
            raise _conflict("artifact idempotency key was reused with different authority")
        if existing.state != "reserved":
            raise _conflict("artifact reservation secret has already been consumed")
        return _reservation_out(existing, settings), True
    descriptor = ToolDescriptor.model_validate(invocation.descriptor_snapshot_json)
    policy = descriptor.artifact_policy
    if policy is None or descriptor.resource_budget.artifact_bytes is None:
        raise _conflict("descriptor does not authorize artifacts")
    if payload.mime_type not in descriptor.execution_permissions.artifacts:
        raise _conflict("artifact MIME is not authorized by the descriptor")
    if payload.mime_type not in SUPPORTED_ARTIFACT_MIMES:
        raise _conflict("artifact MIME has no supported Core inspector")
    live_artifacts = list(
        (
            await session.scalars(
                select(ToolArtifact)
                .where(
                    ToolArtifact.attempt_id == attempt.id,
                    ToolArtifact.state.not_in({"rejected", "expired"}),
                )
                .with_for_update()
            )
        ).all()
    )
    if len(live_artifacts) >= policy.max_count:
        raise _conflict("artifact count exceeds the reviewed policy")
    reserved_bytes = sum(item.max_bytes for item in live_artifacts)
    remaining_bytes = descriptor.resource_budget.artifact_bytes - reserved_bytes
    max_bytes = min(policy.max_single_bytes, remaining_bytes)
    if payload.declared_bytes is not None:
        max_bytes = min(max_bytes, payload.declared_bytes)
    if max_bytes < 1:
        raise _conflict("artifact byte budget is exhausted")
    artifact_id = new_id()
    quarantine_key = store.quarantine_key(artifact_id)
    policy_snapshot, policy_hash = _snapshot(
        {
            "schema_version": "1.0",
            "artifact_policy": policy.model_dump(mode="json"),
            "artifact_budget_bytes": descriptor.resource_budget.artifact_bytes,
            "allowed_mime_types": descriptor.execution_permissions.artifacts,
            "data_classification": descriptor.data_classification,
            "canonical_conversation": invocation.policy_snapshot_json[
                "canonical_conversation"
            ],
            "producer": {
                "tool_id": invocation.tool_id,
                "descriptor_version": invocation.descriptor_version,
                "descriptor_hash": invocation.descriptor_hash,
            },
            "result_retention_seconds": descriptor.result_retention_seconds,
        }
    )
    artifact = ToolArtifact(
        id=artifact_id,
        invocation_id=invocation.id,
        attempt_id=attempt.id,
        provider_id=provider_id,
        fencing_token=attempt.fencing_token,
        idempotency_key=idempotency_key,
        reservation_request_hash=request_hash,
        producer_tool_id=invocation.tool_id,
        producer_descriptor_version=invocation.descriptor_version,
        producer_descriptor_hash=invocation.descriptor_hash,
        data_classification=descriptor.data_classification,
        canonical_conversation=invocation.policy_snapshot_json["canonical_conversation"],
        state="reserved",
        resource_version=1,
        mime_type=payload.mime_type,
        policy_snapshot_json=policy_snapshot,
        policy_hash=policy_hash,
        max_bytes=max_bytes,
        max_width_pixels=policy.max_width_pixels,
        max_height_pixels=policy.max_height_pixels,
        declared_bytes=payload.declared_bytes,
        declared_sha256=payload.declared_sha256,
        expires_at=min(
            now + timedelta(seconds=policy.reservation_ttl_seconds),
            _aware(attempt.deadline_at),
        ),
        upload_secret_hash="0" * 64,
        quarantine_key=quarantine_key,
        created_at=now,
        updated_at=now,
    )
    if _aware(artifact.expires_at) <= now:
        raise _conflict("artifact reservation cannot outlive the current attempt")
    secret = _reservation_secret(artifact, settings)
    artifact.upload_secret_hash = artifact_upload_secret_hash(secret)
    session.add(artifact)
    try:
        await session.flush([artifact])
        await _append_artifact_event(
            session,
            artifact,
            sequence=1,
            event="reserve",
            previous_state=None,
            state="reserved",
            actor_type="provider",
            actor_id=provider_id,
            reason_code="artifact_reserved",
            evidence={
                "request": request_value,
                "request_hash": request_hash,
                "policy_hash": policy_hash,
                "quarantine_key_hash": canonicalize_json_value(
                    {"quarantine_key": quarantine_key}
                ).sha256,
            },
            effective_at=now,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.scalar(
            select(ToolArtifact).where(
                ToolArtifact.provider_id == provider_id,
                ToolArtifact.idempotency_key == idempotency_key,
            )
        )
        if duplicate is not None and duplicate.reservation_request_hash == request_hash:
            if duplicate.state != "reserved":
                raise _conflict("artifact reservation secret has already been consumed") from exc
            return _reservation_out(duplicate, settings), True
        raise _conflict("concurrent artifact reservation conflict") from exc
    return _reservation_out(artifact, settings), False


async def _locked_artifact_context(
    session: AsyncSession,
    artifact_id: str,
    provider_id: str,
) -> tuple[ToolArtifact, ToolAttempt, ToolInvocation, datetime]:
    snapshot = await session.get(ToolArtifact, artifact_id)
    if snapshot is None:
        raise _not_found()
    attempt = await session.scalar(
        select(ToolAttempt).where(ToolAttempt.id == snapshot.attempt_id).with_for_update()
    )
    if attempt is None:
        raise _not_found()
    invocation = await session.scalar(
        select(ToolInvocation)
        .where(ToolInvocation.id == snapshot.invocation_id)
        .with_for_update()
    )
    artifact = await session.scalar(
        select(ToolArtifact).where(ToolArtifact.id == artifact_id).with_for_update()
    )
    if invocation is None or artifact is None:
        raise _not_found()
    now = await database_now(session)
    if artifact.provider_id != provider_id:
        raise _not_found()
    if (
        artifact.attempt_id != attempt.id
        or artifact.invocation_id != invocation.id
        or artifact.fencing_token != attempt.fencing_token
    ):
        raise _conflict("artifact attempt or fence binding is inconsistent")
    return artifact, attempt, invocation, now


async def _reject_artifact(
    session: AsyncSession,
    artifact: ToolArtifact,
    attempt: ToolAttempt,
    *,
    provider_id: str,
    reason_code: str,
    safe_detail: str,
    now: datetime,
) -> None:
    if artifact.state in {"reserved", "uploading"}:
        await _transition_artifact(
            session,
            artifact,
            event="reject",
            state="rejected",
            actor_type="provider",
            actor_id=provider_id,
            reason_code=reason_code,
            evidence={"operation": "artifact_upload", "reason_code": reason_code},
            effective_at=now,
        )
    await _append_attempt_event(
        session,
        attempt,
        event="reject",
        outcome="rejected",
        provider_id=provider_id,
        fencing_token=attempt.fencing_token,
        reason_code=reason_code,
        evidence={"operation": "artifact_upload", "artifact_id": artifact.id},
        now=now,
    )
    await session.commit()
    raise _conflict(safe_detail)


async def upload_tool_artifact(
    session: AsyncSession,
    artifact_id: str,
    chunks: AsyncIterable[bytes],
    provider_id: str,
    upload_secret: str,
    content_type: str,
    content_length: int | None,
    settings: Settings,
) -> ToolArtifactUploadOut:
    store = _store(settings)
    artifact, attempt, invocation, now = await _locked_artifact_context(
        session, artifact_id, provider_id
    )
    if not secrets.compare_digest(
        artifact.upload_secret_hash,
        artifact_upload_secret_hash(upload_secret),
    ):
        await _append_attempt_event(
            session,
            attempt,
            event="reject",
            outcome="rejected",
            provider_id=provider_id,
            fencing_token=attempt.fencing_token,
            reason_code="artifact_upload_secret_mismatch",
            evidence={"operation": "artifact_upload", "artifact_id": artifact.id},
            now=now,
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="artifact upload authorization is invalid",
        )
    if content_type != artifact.mime_type:
        await _append_attempt_event(
            session,
            attempt,
            event="reject",
            outcome="rejected",
            provider_id=provider_id,
            fencing_token=attempt.fencing_token,
            reason_code="artifact_content_type_mismatch",
            evidence={"operation": "artifact_upload", "artifact_id": artifact.id},
            now=now,
        )
        await session.commit()
        raise _conflict("artifact Content-Type does not match its reservation")
    if artifact.state != "reserved":
        raise _conflict("artifact upload secret has already been consumed")
    if (
        attempt.state != "running"
        or invocation.state != "running"
        or _aware(attempt.lease_expires_at) <= now
        or _aware(attempt.deadline_at) <= now
        or _aware(artifact.expires_at) <= now
    ):
        await _reject_artifact(
            session,
            artifact,
            attempt,
            provider_id=provider_id,
            reason_code="artifact_upload_authority_expired",
            safe_detail="artifact upload authority has expired",
            now=now,
        )
    artifact = await _transition_artifact(
        session,
        artifact,
        event="upload_start",
        state="uploading",
        actor_type="provider",
        actor_id=provider_id,
        reason_code="artifact_upload_started",
        evidence={"content_length": content_length},
        effective_at=now,
    )
    await session.commit()
    try:
        uploaded = await store.write_quarantine(
            artifact.quarantine_key,
            chunks,
            max_bytes=artifact.max_bytes,
            max_width_pixels=artifact.max_width_pixels,
            max_height_pixels=artifact.max_height_pixels,
            content_length=content_length,
        )
    except ArtifactStoreError as exc:
        artifact, attempt, _, failed_at = await _locked_artifact_context(
            session, artifact_id, provider_id
        )
        await _reject_artifact(
            session,
            artifact,
            attempt,
            provider_id=provider_id,
            reason_code=exc.code,
            safe_detail=exc.safe_detail,
            now=failed_at,
        )
        raise AssertionError("unreachable")
    artifact, attempt, invocation, completed_at = await _locked_artifact_context(
        session, artifact_id, provider_id
    )
    if (
        artifact.state != "uploading"
        or attempt.state != "running"
        or invocation.state != "running"
        or _aware(attempt.lease_expires_at) <= completed_at
        or _aware(attempt.deadline_at) <= completed_at
        or _aware(artifact.expires_at) <= completed_at
    ):
        await store.remove(artifact.quarantine_key)
        await _reject_artifact(
            session,
            artifact,
            attempt,
            provider_id=provider_id,
            reason_code="artifact_upload_raced_attempt_end",
            safe_detail="artifact upload completed after its attempt ended",
            now=completed_at,
        )
    if (
        (artifact.declared_bytes is not None and artifact.declared_bytes != uploaded.byte_size)
        or (
            artifact.declared_sha256 is not None
            and artifact.declared_sha256 != uploaded.content_sha256
        )
    ):
        await store.remove(artifact.quarantine_key)
        await _reject_artifact(
            session,
            artifact,
            attempt,
            provider_id=provider_id,
            reason_code="artifact_declaration_mismatch",
            safe_detail="artifact body does not match its reservation declaration",
            now=completed_at,
        )
    artifact = await _transition_artifact(
        session,
        artifact,
        event="upload_complete",
        state="uploading",
        actor_type="provider",
        actor_id=provider_id,
        reason_code="artifact_upload_completed",
        evidence={
            "content_sha256": uploaded.content_sha256,
            "byte_size": uploaded.byte_size,
            "mime_type": uploaded.mime_type,
            "width_pixels": uploaded.width_pixels,
            "height_pixels": uploaded.height_pixels,
        },
        effective_at=completed_at,
        values={
            "content_sha256": uploaded.content_sha256,
            "byte_size": uploaded.byte_size,
            "width_pixels": uploaded.width_pixels,
            "height_pixels": uploaded.height_pixels,
        },
    )
    await session.commit()
    return ToolArtifactUploadOut(
        artifact_id=artifact.id,
        state="uploading",
        content_sha256=uploaded.content_sha256,
        mime_type=uploaded.mime_type,
        byte_size=uploaded.byte_size,
        width_pixels=uploaded.width_pixels,
        height_pixels=uploaded.height_pixels,
    )


def _reference(artifact: ToolArtifact) -> ToolArtifactReference:
    if (
        artifact.content_sha256 is None
        or artifact.byte_size is None
        or artifact.width_pixels is None
        or artifact.height_pixels is None
    ):
        raise RuntimeError("finalized artifact lacks observed metadata")
    return ToolArtifactReference(
        artifact_id=artifact.id,
        content_sha256=artifact.content_sha256,
        mime_type=artifact.mime_type,
        byte_size=artifact.byte_size,
        width_pixels=artifact.width_pixels,
        height_pixels=artifact.height_pixels,
    )


async def finalize_tool_artifact(
    session: AsyncSession,
    invocation_id: str,
    payload: ToolArtifactFinalizeIn,
    provider_id: str,
    settings: Settings,
) -> ToolArtifactReference:
    store = _store(settings)
    attempt, invocation, now = await _locked_attempt_and_invocation(
        session,
        invocation_id,
        payload,
        provider_id,
        operation="artifact_finalize",
        allowed_attempt_states={"running"},
        allowed_invocation_states={"running"},
    )
    artifact = await session.scalar(
        select(ToolArtifact).where(ToolArtifact.id == payload.artifact_id).with_for_update()
    )
    if artifact is None or artifact.invocation_id != invocation.id:
        raise _not_found()
    if (
        artifact.provider_id != provider_id
        or artifact.attempt_id != attempt.id
        or artifact.fencing_token != attempt.fencing_token
    ):
        raise _conflict("artifact does not belong to the current fenced attempt")
    if artifact.state != "finalized" and _aware(artifact.expires_at) <= now:
        await _reject_artifact(
            session,
            artifact,
            attempt,
            provider_id=provider_id,
            reason_code="artifact_finalize_authority_expired",
            safe_detail="artifact finalize authority has expired",
            now=now,
        )
    expected = {
        "content_sha256": artifact.content_sha256,
        "mime_type": artifact.mime_type,
        "byte_size": artifact.byte_size,
        "width_pixels": artifact.width_pixels,
        "height_pixels": artifact.height_pixels,
    }
    supplied = payload.model_dump(
        mode="json",
        include={
            "content_sha256",
            "mime_type",
            "byte_size",
            "width_pixels",
            "height_pixels",
        },
    )
    if expected != supplied:
        raise _conflict("artifact finalize metadata does not match Core observations")
    if artifact.state == "finalized":
        if artifact.storage_key is None or artifact.byte_size is None:
            raise _conflict("finalized artifact storage metadata is incomplete")
        if not await store.object_exists(artifact.storage_key, byte_size=artifact.byte_size):
            raise _conflict("finalized artifact object is unavailable")
        return _reference(artifact)
    if artifact.state != "uploading" or artifact.content_sha256 is None or artifact.byte_size is None:
        raise _conflict("artifact upload has not completed")
    async with store.object_lock(artifact.content_sha256):
        try:
            storage_key = await store.finalize(
                artifact.quarantine_key,
                content_sha256=artifact.content_sha256,
                byte_size=artifact.byte_size,
            )
        except ArtifactStoreError as exc:
            await _reject_artifact(
                session,
                artifact,
                attempt,
                provider_id=provider_id,
                reason_code=exc.code,
                safe_detail=exc.safe_detail,
                now=now,
            )
            raise AssertionError("unreachable")
        artifact = await _transition_artifact(
            session,
            artifact,
            event="finalize",
            state="finalized",
            actor_type="provider",
            actor_id=provider_id,
            reason_code="artifact_finalized",
            evidence={
                "content_sha256": artifact.content_sha256,
                "byte_size": artifact.byte_size,
                "storage_key_hash": canonicalize_json_value(
                    {"storage_key": storage_key}
                ).sha256,
            },
            effective_at=now,
            values={"storage_key": storage_key},
        )
        await session.commit()
    return _reference(artifact)


async def validate_and_reference_artifacts(
    session: AsyncSession,
    attempt: ToolAttempt,
    invocation: ToolInvocation,
    descriptor: ToolDescriptor,
    references: list[ToolArtifactReference],
    usage: ToolUsage,
    provider_id: str,
    now: datetime,
    settings: Settings,
    *,
    mark_referenced: bool,
) -> int:
    if len({item.artifact_id for item in references}) != len(references):
        raise ArtifactProtocolError("artifact_duplicate", "artifact references must be unique")
    policy = descriptor.artifact_policy
    if policy is None:
        if references or usage.artifact_bytes != 0:
            raise ArtifactProtocolError(
                "artifact_not_authorized", "descriptor does not authorize artifact output"
            )
        return 0
    if not settings.artifact_enabled:
        raise ArtifactProtocolError(
            "artifact_storage_unavailable", "artifact storage is disabled"
        )
    if len(references) > policy.max_count:
        raise ArtifactProtocolError(
            "artifact_count_exceeded", "artifact result exceeds the reviewed count"
        )
    store = ArtifactStore(settings.artifact_root)
    total_bytes = 0
    rows: list[ToolArtifact] = []
    for reference in sorted(references, key=lambda item: item.artifact_id):
        artifact = await session.scalar(
            select(ToolArtifact)
            .where(ToolArtifact.id == reference.artifact_id)
            .with_for_update()
        )
        if artifact is None:
            raise ArtifactProtocolError("artifact_missing", "artifact reference is unknown")
        if (
            artifact.invocation_id != invocation.id
            or artifact.attempt_id != attempt.id
            or artifact.provider_id != provider_id
            or artifact.fencing_token != attempt.fencing_token
            or artifact.state != "finalized"
            or artifact.content_deleted_at is not None
        ):
            raise ArtifactProtocolError(
                "artifact_binding_mismatch",
                "artifact is not finalized for the current fenced attempt",
            )
        if _reference(artifact) != reference:
            raise ArtifactProtocolError(
                "artifact_metadata_mismatch", "artifact reference metadata is not exact"
            )
        if artifact.storage_key is None or artifact.byte_size is None:
            raise ArtifactProtocolError(
                "artifact_storage_missing", "artifact storage metadata is incomplete"
            )
        if not await store.object_exists(artifact.storage_key, byte_size=artifact.byte_size):
            raise ArtifactProtocolError(
                "artifact_object_missing", "artifact content-addressed object is unavailable"
            )
        total_bytes += artifact.byte_size
        rows.append(artifact)
    if total_bytes != usage.artifact_bytes:
        raise ArtifactProtocolError(
            "artifact_usage_mismatch",
            "reported artifact bytes do not match finalized references",
            {"actual_artifact_bytes": total_bytes},
        )
    if descriptor.resource_budget.artifact_bytes is not None and total_bytes > (
        descriptor.resource_budget.artifact_bytes
    ):
        raise ArtifactProtocolError(
            "artifact_budget_exceeded", "artifact references exceed the reviewed byte budget"
        )
    if mark_referenced:
        for artifact in rows:
            await _transition_artifact(
                session,
                artifact,
                event="reference",
                state="finalized",
                actor_type="provider",
                actor_id=provider_id,
                reason_code="artifact_referenced_by_completion",
                evidence={
                    "attempt_id": attempt.id,
                    "provider_result_reference": True,
                    "content_sha256": artifact.content_sha256,
                },
                effective_at=now,
            )
    return total_bytes


async def reap_expired_artifacts(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = 100,
) -> list[str]:
    if not settings.artifact_enabled:
        return []
    store = ArtifactStore(settings.artifact_root)
    now = await database_now(session)
    candidate_ids = list(
        (
            await session.scalars(
                select(ToolArtifact.id)
                .where(
                    ToolArtifact.state.in_({"reserved", "uploading"}),
                    ToolArtifact.expires_at <= now,
                )
                .order_by(ToolArtifact.expires_at, ToolArtifact.created_at)
                .limit(max(1, min(limit, 1_000)))
            )
        ).all()
    )
    reaped: list[str] = []
    for artifact_id in candidate_ids:
        snapshot = await session.get(ToolArtifact, artifact_id)
        if snapshot is None:
            continue
        attempt = await session.scalar(
            select(ToolAttempt).where(ToolAttempt.id == snapshot.attempt_id).with_for_update()
        )
        artifact = await session.scalar(
            select(ToolArtifact).where(ToolArtifact.id == artifact_id).with_for_update()
        )
        if (
            attempt is None
            or artifact is None
            or artifact.state not in {"reserved", "uploading"}
            or _aware(artifact.expires_at) > now
        ):
            continue
        artifact = await _transition_artifact(
            session,
            artifact,
            event="expire",
            state="expired",
            actor_type="reaper",
            actor_id="tool-artifact-reaper-v1",
            reason_code="artifact_reservation_expired",
            evidence={"database_time": now.isoformat(), "attempt_state": attempt.state},
            effective_at=now,
        )
        removed = await store.remove(artifact.quarantine_key)
        if removed:
            await _transition_artifact(
                session,
                artifact,
                event="cleanup",
                state="expired",
                actor_type="reaper",
                actor_id="tool-artifact-reaper-v1",
                reason_code="artifact_quarantine_cleaned",
                evidence={"storage_class": "quarantine"},
                effective_at=now,
            )
        reaped.append(artifact.id)
    await session.commit()

    cleanup_ids = list(
        (
            await session.scalars(
                select(ToolArtifact.id)
                .where(
                    ToolArtifact.state == "finalized",
                    ToolArtifact.content_deleted_at.is_(None),
                )
                .order_by(ToolArtifact.finalized_at, ToolArtifact.created_at)
                .limit(max(1, min(limit, 1_000)))
            )
        ).all()
    )
    for artifact_id in cleanup_ids:
        artifact = await session.scalar(
            select(ToolArtifact).where(ToolArtifact.id == artifact_id).with_for_update()
        )
        if (
            artifact is None
            or artifact.state != "finalized"
            or artifact.content_deleted_at is not None
            or artifact.storage_key is None
            or artifact.content_sha256 is None
        ):
            continue
        attempt = await session.get(ToolAttempt, artifact.attempt_id)
        if attempt is None:
            continue
        reason_code: str | None = None
        if artifact.referenced_at is not None:
            retention = artifact.policy_snapshot_json.get("result_retention_seconds")
            if not isinstance(retention, int) or isinstance(retention, bool) or retention < 0:
                continue
            if _aware(artifact.referenced_at) + timedelta(seconds=retention) <= now:
                reason_code = "artifact_retention_elapsed"
        elif (
            attempt.state in _TERMINAL_ATTEMPT_STATES
            and artifact.finalized_at is not None
            and _aware(artifact.finalized_at)
            + timedelta(seconds=settings.artifact_orphan_grace_seconds)
            <= now
        ):
            reason_code = "artifact_unreferenced_after_terminal_attempt"
        if reason_code is None:
            continue
        async with store.object_lock(artifact.content_sha256):
            blockers = await session.scalar(
                select(func.count(ToolArtifact.id)).where(
                    ToolArtifact.id != artifact.id,
                    ToolArtifact.content_sha256 == artifact.content_sha256,
                    ToolArtifact.state.in_({"uploading", "finalized"}),
                    ToolArtifact.content_deleted_at.is_(None),
                )
            )
            render_blockers = await session.scalar(
                select(func.count(RenderArtifactRecord.id)).where(
                    RenderArtifactRecord.content_sha256 == artifact.content_sha256,
                    RenderArtifactRecord.expires_at > now,
                    RenderArtifactRecord.content_deleted_at.is_(None),
                )
            )
            artifact = await _transition_artifact(
                session,
                artifact,
                event="cleanup",
                state="finalized",
                actor_type="reaper",
                actor_id="tool-artifact-reaper-v1",
                reason_code=reason_code,
                evidence={
                    "storage_class": "content_addressed",
                    "physical_delete_candidate": not bool(blockers or render_blockers),
                },
                effective_at=now,
            )
            await session.commit()
            if not blockers and not render_blockers:
                await store.remove(artifact.storage_key)

    known_keys = set(
        (
            await session.scalars(
                select(ToolArtifact.storage_key).where(
                    ToolArtifact.storage_key.is_not(None),
                    ToolArtifact.content_deleted_at.is_(None),
                )
            )
        ).all()
    )
    known_keys.update(
        (
            await session.scalars(
                select(RenderArtifactRecord.storage_key).where(
                    RenderArtifactRecord.expires_at > now,
                    RenderArtifactRecord.content_deleted_at.is_(None),
                )
            )
        ).all()
    )
    orphan_cutoff = time.time() - settings.artifact_orphan_grace_seconds
    for key, modified_at in await store.list_objects():
        if key not in known_keys and modified_at <= orphan_cutoff:
            digest = store.digest_from_object_key(key)
            async with store.object_lock(digest):
                live_rows = await session.scalar(
                    select(func.count(ToolArtifact.id)).where(
                        ToolArtifact.content_sha256 == digest,
                        ToolArtifact.state.in_({"uploading", "finalized"}),
                        ToolArtifact.content_deleted_at.is_(None),
                    )
                )
                live_render_rows = await session.scalar(
                    select(func.count(RenderArtifactRecord.id)).where(
                        RenderArtifactRecord.content_sha256 == digest,
                        RenderArtifactRecord.expires_at > now,
                        RenderArtifactRecord.content_deleted_at.is_(None),
                    )
                )
                await session.commit()
                if not live_rows and not live_render_rows:
                    await store.remove_if_older(
                        key,
                        cutoff_timestamp=orphan_cutoff,
                    )
    return reaped
