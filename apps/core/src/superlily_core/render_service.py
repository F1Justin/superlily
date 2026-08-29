"""Core-owned render orchestration, artifact publication, and delivery evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    CapabilityPlanningError,
    DeliveryAttemptIn,
    DeliveryCompletionIn,
    DeliveryIntentIn,
    DeliveryPlanDecision,
    RenderArtifactDeletionIn,
    RenderContentDiagnostic,
    RenderDocument,
    canonicalize_json_value,
    plan_render_delivery,
    render_document_hash,
    render_document_plain_text,
)

from .artifact_store import ArtifactStore, ArtifactStoreError
from .document_renderer_client import DocumentRendererClient, DocumentRendererError
from .models import (
    BotInstance,
    RenderArtifactRecord,
    RenderAttemptRecord,
    RenderDeliveryAttempt,
    RenderDeliveryIntent,
    RenderDeliveryPlan,
    RenderDocumentRecord,
    ToolArtifact,
    utc_now,
)
from .settings import Settings


class RenderServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        safe_detail: str,
        diagnostic: RenderContentDiagnostic | None = None,
    ) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail
        self.diagnostic = diagnostic


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content


def _attempt_duration(started: float) -> int:
    return min(120_000, round((monotonic() - started) * 1_000))


def _renderer_snapshot(
    settings: Settings,
    document: RenderDocument,
    decision: DeliveryPlanDecision,
) -> dict[str, str]:
    return {
        "profile": "xelatex-document-v1",
        "implementation_hash": settings.render_implementation_hash,
        "document_schema_version": document.schema_version,
        "capability_hash": decision.capability_hash,
        "delivery_decision_hash": decision.decision_hash,
        "resolved_document_hash": decision.resolved_document_hash,
    }


async def _get_or_create_document(
    session: AsyncSession,
    document: RenderDocument,
    idempotency_key: str,
    request_sha256: str,
) -> tuple[RenderDocumentRecord, bool]:
    existing = await session.scalar(
        select(RenderDocumentRecord).where(
            RenderDocumentRecord.instance_id == document.instance_id,
            RenderDocumentRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_sha256 != request_sha256:
            raise RenderServiceError("idempotency_conflict", "idempotency key was reused")
        return existing, True

    record = RenderDocumentRecord(
        instance_id=document.instance_id,
        conversation_key=document.conversation_key,
        source_event_id=document.source_event_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        document_json=document.model_dump(mode="json"),
    )
    session.add(record)
    try:
        await session.commit()
        return record, False
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(
            select(RenderDocumentRecord).where(
                RenderDocumentRecord.instance_id == document.instance_id,
                RenderDocumentRecord.idempotency_key == idempotency_key,
            )
        )
        if winner is None:
            raise
        if winner.request_sha256 != request_sha256:
            raise RenderServiceError("idempotency_conflict", "idempotency key was reused")
        return winner, True


async def _latest_attempt(
    session: AsyncSession, render_id: str
) -> RenderAttemptRecord | None:
    return await session.scalar(
        select(RenderAttemptRecord)
        .where(RenderAttemptRecord.render_id == render_id)
        .order_by(desc(RenderAttemptRecord.attempt_number))
        .limit(1)
    )


async def _live_artifact(
    session: AsyncSession,
    settings: Settings,
    render_id: str,
    now: datetime,
    renderer_snapshot_hash: str,
) -> RenderArtifactRecord | None:
    rows = (
        await session.execute(
            select(RenderArtifactRecord, RenderAttemptRecord)
            .join(
                RenderAttemptRecord,
                RenderAttemptRecord.id == RenderArtifactRecord.attempt_id,
            )
            .where(
                RenderArtifactRecord.render_id == render_id,
                RenderArtifactRecord.expires_at > now,
                RenderArtifactRecord.content_deleted_at.is_(None),
            )
            .order_by(desc(RenderArtifactRecord.created_at))
        )
    ).all()
    store = ArtifactStore(settings.artifact_root)
    for artifact, attempt in rows:
        # Reuse is deliberately scoped to one canonical RenderDocument record
        # and only valid for the exact deterministic renderer inputs.  The
        # snapshot hash binds the schema/input decision, capability profile,
        # renderer profile, and implementation/font bundle identity.
        if attempt.renderer_snapshot_hash != renderer_snapshot_hash:
            continue
        try:
            if (
                store.digest_from_object_key(artifact.storage_key) == artifact.content_sha256
                and await store.object_exists(
                    artifact.storage_key, byte_size=artifact.byte_size
                )
            ):
                return artifact
        except ArtifactStoreError:
            continue
    return None


def _capability_snapshot(instance: BotInstance) -> dict:
    raw = instance.metadata_json.get("capabilities", {})
    if not isinstance(raw, dict):
        raw = {}
    supported = raw.get("supported", [])
    normalized_supported = sorted(
        {item for item in supported if isinstance(item, str) and 0 < len(item) <= 64}
    )
    limits = raw.get("limits", {})
    return {
        "profile": raw.get("profile") if isinstance(raw.get("profile"), str) else "unknown",
        "supported": normalized_supported,
        "limits": limits if isinstance(limits, dict) else {},
    }


async def get_or_create_delivery_plan(
    session: AsyncSession,
    document: RenderDocument,
    artifact: RenderArtifactRecord,
    decision: DeliveryPlanDecision,
) -> RenderDeliveryPlan:
    existing = await session.scalar(
        select(RenderDeliveryPlan).where(
            RenderDeliveryPlan.artifact_id == artifact.id,
            RenderDeliveryPlan.capability_hash == decision.capability_hash,
        )
    )
    if existing is not None:
        return existing

    plan = RenderDeliveryPlan(
        artifact_id=artifact.id,
        instance_id=document.instance_id,
        conversation_key=document.conversation_key,
        capability_snapshot_json=decision.capability_snapshot,
        capability_hash=decision.capability_hash,
        selected_family=decision.selected_family,
        fallback_text=decision.fallback_text,
        degradation_reasons_json=list(decision.degradation_reasons),
        decision_hash=decision.decision_hash,
        resolved_document_hash=decision.resolved_document_hash,
        selected_alternatives_json=[
            item.as_json() for item in decision.selected_alternatives
        ],
        rejected_alternatives_json=[
            item.as_json() for item in decision.rejected_alternatives
        ],
        ordered_payloads_json=[
            item.as_json() for item in decision.ordered_payloads
        ],
        expires_at=artifact.expires_at,
    )
    session.add(plan)
    try:
        await session.commit()
        return plan
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(
            select(RenderDeliveryPlan).where(
                RenderDeliveryPlan.artifact_id == artifact.id,
                RenderDeliveryPlan.capability_hash == decision.capability_hash,
            )
        )
        if winner is None:
            raise
        return winner


async def _mark_attempt_failed(
    session: AsyncSession,
    record: RenderDocumentRecord,
    attempt: RenderAttemptRecord,
    error_code: str,
    duration_ms: int,
) -> None:
    now = utc_now()
    await session.refresh(attempt)
    if attempt.state != "running":
        return
    attempt.state = "failed"
    attempt.safe_error_code = error_code
    attempt.render_duration_ms = duration_ms
    attempt.completed_at = now
    await session.refresh(record)
    record.status = "failed"
    record.safe_error_code = error_code
    record.render_duration_ms = duration_ms
    record.completed_at = now
    await session.commit()


async def submit_render_document(
    session: AsyncSession,
    settings: Settings,
    document: RenderDocument,
    idempotency_key: str,
) -> tuple[
    RenderDocumentRecord,
    RenderAttemptRecord,
    RenderArtifactRecord,
    RenderDeliveryPlan,
    bool,
]:
    if not settings.render_enabled:
        raise RenderServiceError("render_disabled", "document rendering is disabled")
    if not settings.render_conversation_allowed(document.conversation_key):
        raise RenderServiceError(
            "render_conversation_forbidden", "conversation is not allowed to render"
        )

    instance = await session.get(BotInstance, document.instance_id)
    if instance is None:
        raise RenderServiceError("instance_not_found", "render instance was not found")
    try:
        decision = plan_render_delivery(document, _capability_snapshot(instance))
    except CapabilityPlanningError as exc:
        raise RenderServiceError(exc.code, exc.safe_detail) from exc

    request_sha256 = render_document_hash(document)
    snapshot = _renderer_snapshot(settings, document, decision)
    snapshot_hash = canonicalize_json_value(snapshot).sha256
    record, _duplicate_document = await _get_or_create_document(
        session, document, idempotency_key, request_sha256
    )
    record_id = record.id
    now = utc_now()
    await session.rollback()
    record = await session.scalar(
        select(RenderDocumentRecord)
        .where(RenderDocumentRecord.id == record_id)
        .with_for_update()
    )
    assert record is not None
    artifact = await _live_artifact(
        session,
        settings,
        record.id,
        now,
        snapshot_hash,
    )
    if artifact is not None:
        attempt = await session.get(RenderAttemptRecord, artifact.attempt_id)
        if attempt is None:
            raise RenderServiceError("attempt_missing", "render attempt is unavailable")
        await session.commit()
        plan = await get_or_create_delivery_plan(
            session, document, artifact, decision
        )
        return record, attempt, artifact, plan, True

    latest = await _latest_attempt(session, record.id)
    if latest is not None and latest.state == "running":
        if _as_utc(latest.lease_expires_at) > now:
            await session.rollback()
            raise RenderServiceError("render_in_progress", "render request is still running")
        latest.state = "abandoned"
        latest.safe_error_code = "render_lease_expired"
        latest.completed_at = now

    next_number = (latest.attempt_number if latest is not None else 0) + 1
    attempt = RenderAttemptRecord(
        render_id=record.id,
        attempt_number=next_number,
        fencing_token=next_number,
        state="running",
        renderer_profile=snapshot["profile"],
        renderer_snapshot_json=snapshot,
        renderer_snapshot_hash=snapshot_hash,
        lease_expires_at=now + timedelta(seconds=settings.render_timeout_seconds + 10),
        started_at=now,
    )
    record.status = "pending"
    record.safe_error_code = None
    record.render_duration_ms = None
    record.completed_at = None
    session.add(attempt)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        winner = await _latest_attempt(session, record_id)
        if winner is not None:
            raise RenderServiceError(
                "render_in_progress", "another render attempt won the execution fence"
            ) from exc
        raise
    attempt_id = attempt.id

    render_started = monotonic()
    try:
        worker = DocumentRendererClient(
            settings.render_backend_url,
            settings.render_backend_token,
        )
        result = await worker.render_document(
            decision.resolved_document,
            timeout_seconds=float(settings.render_timeout_seconds),
        )
        duration_ms = _attempt_duration(render_started)

        await session.rollback()
        locked_record = await session.scalar(
            select(RenderDocumentRecord)
            .where(RenderDocumentRecord.id == record_id)
            .with_for_update()
        )
        locked_attempt = await session.get(RenderAttemptRecord, attempt_id)
        current = await _latest_attempt(session, record_id)
        if (
            locked_record is None
            or locked_attempt is None
            or locked_attempt.state != "running"
            or current is None
            or current.fencing_token != locked_attempt.fencing_token
        ):
            await session.rollback()
            raise RenderServiceError("render_attempt_superseded", "render attempt was superseded")

        artifact_id = str(uuid4())
        store = ArtifactStore(settings.artifact_root)
        quarantine_key = store.quarantine_key(artifact_id)
        upload = await store.write_quarantine(
            quarantine_key,
            _one_chunk(result.content),
            max_bytes=8_388_608,
            max_width_pixels=4_096,
            max_height_pixels=4_096,
            content_length=len(result.content),
        )
        if upload.content_sha256 != result.content_sha256:
            raise RenderServiceError("renderer_integrity_failure", "renderer artifact hash changed")
        storage_key = await store.finalize(
            quarantine_key,
            content_sha256=upload.content_sha256,
            byte_size=upload.byte_size,
        )
        completed = utc_now()
        expires_at = completed + timedelta(
            seconds=settings.render_artifact_ttl_seconds
        )
        artifact = RenderArtifactRecord(
            id=artifact_id,
            render_id=locked_record.id,
            attempt_id=locked_attempt.id,
            content_sha256=upload.content_sha256,
            storage_key=storage_key,
            mime_type=upload.mime_type,
            byte_size=upload.byte_size,
            width_pixels=upload.width_pixels,
            height_pixels=upload.height_pixels,
            producer_kind="document_renderer",
            producer_id=snapshot["profile"],
            data_classification="conversation",
            canonical_scope=document.conversation_key,
            safe_filename=f"render-{locked_record.id}.png",
            accessibility_text=render_document_plain_text(
                decision.resolved_document
            )[:2_000],
            created_at=completed,
            expires_at=expires_at,
            retention_until=expires_at,
        )
        locked_attempt.state = "succeeded"
        locked_attempt.render_duration_ms = duration_ms
        locked_attempt.completed_at = completed
        locked_record.status = "succeeded"
        locked_record.safe_error_code = None
        locked_record.render_duration_ms = duration_ms
        locked_record.completed_at = completed
        session.add(artifact)
        await session.commit()
        plan = await get_or_create_delivery_plan(
            session, document, artifact, decision
        )
        return locked_record, locked_attempt, artifact, plan, False
    except RenderServiceError as exc:
        await _mark_attempt_failed(
            session,
            record,
            attempt,
            exc.code,
            _attempt_duration(render_started),
        )
        raise
    except DocumentRendererError as exc:
        error_code = (
            "renderer_content_error"
            if exc.diagnostic is not None
            else f"renderer_{exc.error_code}"
        )
        await _mark_attempt_failed(
            session, record, attempt, error_code, _attempt_duration(render_started)
        )
        raise RenderServiceError(
            error_code,
            "document renderer failed safely",
            exc.diagnostic,
        ) from exc
    except ArtifactStoreError as exc:
        await _mark_attempt_failed(
            session,
            record,
            attempt,
            "artifact_store_failure",
            _attempt_duration(render_started),
        )
        raise RenderServiceError(
            "artifact_store_failure", "render artifact could not be published"
        ) from exc


async def submit_passthrough_render_document(
    session: AsyncSession,
    settings: Settings,
    document: RenderDocument,
    idempotency_key: str,
    *,
    source_artifact: ToolArtifact,
    source_invocation_id: str,
    producer_id: str,
) -> tuple[
    RenderDocumentRecord,
    RenderAttemptRecord,
    RenderArtifactRecord,
    RenderDeliveryPlan,
    bool,
]:
    """Publish a reviewed tool PNG as a render artifact without recompression."""

    if not settings.render_enabled:
        raise RenderServiceError("render_disabled", "document rendering is disabled")
    if not settings.render_conversation_allowed(document.conversation_key):
        raise RenderServiceError(
            "render_conversation_forbidden", "conversation is not allowed to render"
        )
    if (
        source_artifact.state != "finalized"
        or source_artifact.content_deleted_at is not None
        or source_artifact.storage_key is None
        or source_artifact.content_sha256 is None
        or source_artifact.byte_size is None
        or source_artifact.width_pixels is None
        or source_artifact.height_pixels is None
        or source_artifact.mime_type != "image/png"
    ):
        raise RenderServiceError(
            "source_artifact_unavailable",
            "tool result artifact is not available for rendering",
        )

    instance = await session.get(BotInstance, document.instance_id)
    if instance is None:
        raise RenderServiceError("instance_not_found", "render instance was not found")
    try:
        decision = plan_render_delivery(document, _capability_snapshot(instance))
    except CapabilityPlanningError as exc:
        raise RenderServiceError(exc.code, exc.safe_detail) from exc

    request_sha256 = render_document_hash(document)
    snapshot = {
        "profile": "tool-artifact-passthrough-v1",
        "implementation_hash": source_artifact.producer_descriptor_hash,
        "document_schema_version": document.schema_version,
        "capability_hash": decision.capability_hash,
        "delivery_decision_hash": decision.decision_hash,
        "resolved_document_hash": decision.resolved_document_hash,
        "source_invocation_id": source_invocation_id,
        "source_artifact_id": source_artifact.id,
    }
    snapshot_hash = canonicalize_json_value(snapshot).sha256
    record, _ = await _get_or_create_document(
        session, document, idempotency_key, request_sha256
    )
    record_id = record.id
    now = utc_now()
    await session.rollback()
    record = await session.scalar(
        select(RenderDocumentRecord)
        .where(RenderDocumentRecord.id == record_id)
        .with_for_update()
    )
    assert record is not None
    artifact = await _live_artifact(
        session,
        settings,
        record.id,
        now,
        snapshot_hash,
    )
    if artifact is not None:
        attempt = await session.get(RenderAttemptRecord, artifact.attempt_id)
        if attempt is None:
            raise RenderServiceError("attempt_missing", "render attempt is unavailable")
        await session.commit()
        plan = await get_or_create_delivery_plan(
            session, document, artifact, decision
        )
        return record, attempt, artifact, plan, True

    latest = await _latest_attempt(session, record.id)
    if latest is not None and latest.state == "running":
        if _as_utc(latest.lease_expires_at) > now:
            await session.rollback()
            raise RenderServiceError(
                "render_in_progress", "render request is still running"
            )
        latest.state = "abandoned"
        latest.safe_error_code = "render_lease_expired"
        latest.completed_at = now

    retention_seconds = source_artifact.policy_snapshot_json.get(
        "result_retention_seconds"
    )
    if (
        not isinstance(retention_seconds, int)
        or isinstance(retention_seconds, bool)
        or retention_seconds < 1
    ):
        raise RenderServiceError(
            "source_artifact_policy_invalid",
            "tool result artifact retention policy is invalid",
        )
    retention_origin = source_artifact.referenced_at or source_artifact.finalized_at
    if retention_origin is None:
        raise RenderServiceError(
            "source_artifact_unavailable",
            "tool result artifact is not referenced by a completed result",
        )
    source_retention_until = _as_utc(retention_origin) + timedelta(
        seconds=retention_seconds
    )
    expires_at = min(
        now + timedelta(seconds=settings.render_artifact_ttl_seconds),
        source_retention_until,
    )
    if expires_at <= now:
        raise RenderServiceError(
            "source_artifact_expired", "tool result artifact retention has elapsed"
        )
    store = ArtifactStore(settings.artifact_root)
    if not await store.object_exists(
        source_artifact.storage_key,
        byte_size=source_artifact.byte_size,
    ):
        raise RenderServiceError(
            "source_artifact_unavailable",
            "tool result artifact bytes are unavailable",
        )

    next_number = (latest.attempt_number if latest is not None else 0) + 1
    completed = utc_now()
    attempt = RenderAttemptRecord(
        id=str(uuid4()),
        render_id=record.id,
        attempt_number=next_number,
        fencing_token=next_number,
        state="succeeded",
        renderer_profile=snapshot["profile"],
        renderer_snapshot_json=snapshot,
        renderer_snapshot_hash=snapshot_hash,
        lease_expires_at=completed,
        render_duration_ms=0,
        started_at=completed,
        completed_at=completed,
    )
    artifact = RenderArtifactRecord(
        id=str(uuid4()),
        render_id=record.id,
        attempt_id=attempt.id,
        content_sha256=source_artifact.content_sha256,
        storage_key=source_artifact.storage_key,
        mime_type=source_artifact.mime_type,
        byte_size=source_artifact.byte_size,
        width_pixels=source_artifact.width_pixels,
        height_pixels=source_artifact.height_pixels,
        producer_kind="tool_artifact_passthrough",
        producer_id=producer_id,
        source_invocation_id=source_invocation_id,
        data_classification=source_artifact.data_classification,
        canonical_scope=document.conversation_key,
        safe_filename=f"{source_artifact.producer_tool_id}-result.png",
        accessibility_text=render_document_plain_text(
            decision.resolved_document
        )[:2_000],
        created_at=completed,
        expires_at=expires_at,
        retention_until=expires_at,
    )
    record.status = "succeeded"
    record.safe_error_code = None
    record.render_duration_ms = 0
    record.completed_at = completed
    # The models deliberately do not expose mutable ORM relationships. Flush
    # the fenced attempt first so PostgreSQL can enforce the artifact FK in the
    # same transaction; SQLite's deferred behavior otherwise hides this order.
    session.add(attempt)
    try:
        await session.flush()
        session.add(artifact)
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise RenderServiceError(
            "render_in_progress", "another passthrough attempt won the execution fence"
        ) from exc
    plan = await get_or_create_delivery_plan(
        session, document, artifact, decision
    )
    return record, attempt, artifact, plan, False


async def get_render_artifact(
    session: AsyncSession,
    settings: Settings,
    artifact_id: str,
    instance_id: str,
) -> tuple[RenderArtifactRecord, bytes]:
    row = await session.execute(
        select(RenderArtifactRecord, RenderDocumentRecord)
        .join(RenderDocumentRecord, RenderDocumentRecord.id == RenderArtifactRecord.render_id)
        .where(RenderArtifactRecord.id == artifact_id)
    )
    result = row.one_or_none()
    if result is None:
        raise RenderServiceError("artifact_not_found", "render artifact was not found")
    artifact, document = result
    if document.instance_id != instance_id:
        raise RenderServiceError("artifact_forbidden", "render artifact belongs to another instance")
    if _as_utc(artifact.expires_at) <= utc_now():
        raise RenderServiceError("artifact_expired", "render artifact has expired")
    if artifact.content_deleted_at is not None:
        raise RenderServiceError("artifact_deleted", "render artifact content was deleted")
    content = await ArtifactStore(settings.artifact_root).read_object(
        artifact.storage_key,
        byte_size=artifact.byte_size,
        content_sha256=artifact.content_sha256,
    )
    return artifact, content


async def delete_render_artifact_content(
    session: AsyncSession,
    settings: Settings,
    artifact_id: str,
    authenticated_instance: str,
    payload: RenderArtifactDeletionIn,
) -> tuple[RenderArtifactRecord, bool, bool]:
    """Logically delete one artifact and remove unshared bytes under a digest lock."""

    if payload.instance_id != authenticated_instance:
        raise RenderServiceError("artifact_forbidden", "artifact identity does not match token")
    row = await session.execute(
        select(RenderArtifactRecord, RenderDocumentRecord)
        .join(RenderDocumentRecord, RenderDocumentRecord.id == RenderArtifactRecord.render_id)
        .where(RenderArtifactRecord.id == artifact_id)
        .with_for_update()
    )
    result = row.one_or_none()
    if result is None:
        raise RenderServiceError("artifact_not_found", "render artifact was not found")
    artifact, document = result
    if document.instance_id != authenticated_instance:
        raise RenderServiceError("artifact_forbidden", "render artifact belongs to another instance")
    if artifact.content_deleted_at is not None:
        return artifact, False, True

    store = ArtifactStore(settings.artifact_root)
    now = utc_now()
    physical_removed = False
    async with store.object_lock(artifact.content_sha256):
        tool_blockers = await session.scalar(
            select(func.count(ToolArtifact.id)).where(
                ToolArtifact.content_sha256 == artifact.content_sha256,
                ToolArtifact.state.in_({"uploading", "finalized"}),
                ToolArtifact.content_deleted_at.is_(None),
            )
        )
        render_blockers = await session.scalar(
            select(func.count(RenderArtifactRecord.id)).where(
                RenderArtifactRecord.id != artifact.id,
                RenderArtifactRecord.content_sha256 == artifact.content_sha256,
                RenderArtifactRecord.content_deleted_at.is_(None),
                RenderArtifactRecord.retention_until > now,
            )
        )
        artifact.content_deleted_at = now
        artifact.deletion_reason = payload.reason
        await session.commit()
        if not tool_blockers and not render_blockers:
            physical_removed = await store.remove(artifact.storage_key)
    return artifact, physical_removed, False


async def record_delivery_attempt(
    session: AsyncSession,
    artifact_id: str,
    authenticated_instance: str,
    payload: DeliveryAttemptIn,
) -> RenderDeliveryAttempt:
    if payload.instance_id != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "delivery identity does not match token")
    owner = await session.scalar(
        select(RenderDocumentRecord.instance_id)
        .join(RenderArtifactRecord, RenderArtifactRecord.render_id == RenderDocumentRecord.id)
        .where(RenderArtifactRecord.id == artifact_id)
    )
    if owner is None:
        raise RenderServiceError("artifact_not_found", "render artifact was not found")
    if owner != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "render artifact belongs to another instance")
    attempt = RenderDeliveryAttempt(
        artifact_id=artifact_id,
        instance_id=authenticated_instance,
        plan_id=payload.delivery_plan_id,
        intent_id=payload.delivery_intent_id,
        outcome=payload.outcome,
        platform_message_id=payload.platform_message_id,
        safe_error_code=payload.safe_error_code,
    )
    session.add(attempt)
    await session.commit()
    return attempt


async def create_delivery_intent(
    session: AsyncSession,
    settings: Settings,
    artifact_id: str,
    authenticated_instance: str,
    payload: DeliveryIntentIn,
) -> tuple[RenderDeliveryIntent, bool]:
    if payload.instance_id != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "delivery identity does not match token")
    plan = await session.get(RenderDeliveryPlan, payload.delivery_plan_id)
    if plan is None or plan.artifact_id != artifact_id:
        raise RenderServiceError("delivery_plan_not_found", "delivery plan was not found")
    if plan.instance_id != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "delivery plan belongs to another instance")
    existing = await session.scalar(
        select(RenderDeliveryIntent).where(
            RenderDeliveryIntent.instance_id == authenticated_instance,
            RenderDeliveryIntent.idempotency_key == payload.idempotency_key,
        )
    )
    now = utc_now()
    if existing is not None:
        if (
            existing.plan_id != plan.id
            or existing.reply_to_platform_message_id
            != payload.reply_to_platform_message_id
            or existing.mention_ids_json != payload.mention_ids
        ):
            raise RenderServiceError("idempotency_conflict", "delivery key was reused")
        if existing.status == "pending" and _as_utc(existing.deadline_at) <= now:
            existing.status = "ambiguous"
            existing.safe_error_code = "platform_completion_unknown"
            existing.completed_at = now
            session.add(
                RenderDeliveryAttempt(
                    artifact_id=artifact_id,
                    instance_id=authenticated_instance,
                    plan_id=plan.id,
                    intent_id=existing.id,
                    outcome="ambiguous",
                    safe_error_code="platform_completion_unknown",
                )
            )
            await session.commit()
        return existing, False

    intent = RenderDeliveryIntent(
        plan_id=plan.id,
        instance_id=authenticated_instance,
        conversation_key=plan.conversation_key,
        capability_hash=plan.capability_hash,
        ordered_payloads_json=plan.ordered_payloads_json,
        reply_to_platform_message_id=payload.reply_to_platform_message_id,
        mention_ids_json=payload.mention_ids,
        idempotency_key=payload.idempotency_key,
        status="pending",
        deadline_at=now + timedelta(seconds=settings.render_delivery_intent_seconds),
        created_at=now,
    )
    session.add(intent)
    try:
        await session.commit()
        return intent, True
    except IntegrityError:
        await session.rollback()
        winner = await session.scalar(
            select(RenderDeliveryIntent).where(
                RenderDeliveryIntent.instance_id == authenticated_instance,
                RenderDeliveryIntent.idempotency_key == payload.idempotency_key,
            )
        )
        if winner is None:
            raise
        if (
            winner.plan_id != plan.id
            or winner.reply_to_platform_message_id
            != payload.reply_to_platform_message_id
            or winner.mention_ids_json != payload.mention_ids
        ):
            raise RenderServiceError("idempotency_conflict", "delivery key was reused")
        return winner, False


async def complete_delivery_intent(
    session: AsyncSession,
    intent_id: str,
    authenticated_instance: str,
    payload: DeliveryCompletionIn,
) -> tuple[RenderDeliveryIntent, RenderDeliveryAttempt, bool]:
    if payload.instance_id != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "delivery identity does not match token")
    intent = await session.scalar(
        select(RenderDeliveryIntent)
        .where(RenderDeliveryIntent.id == intent_id)
        .with_for_update()
    )
    if intent is None:
        raise RenderServiceError("delivery_intent_not_found", "delivery intent was not found")
    if intent.instance_id != authenticated_instance:
        raise RenderServiceError("delivery_forbidden", "delivery intent belongs to another instance")
    plan = await session.get(RenderDeliveryPlan, intent.plan_id)
    assert plan is not None
    existing_attempt = await session.scalar(
        select(RenderDeliveryAttempt).where(RenderDeliveryAttempt.intent_id == intent.id)
    )
    if intent.status != "pending":
        same = (
            intent.status == payload.outcome
            and intent.platform_message_id == payload.platform_message_id
            and intent.safe_error_code == payload.safe_error_code
        )
        if not same or existing_attempt is None:
            raise RenderServiceError("delivery_completion_conflict", "delivery is already terminal")
        return intent, existing_attempt, True

    now = utc_now()
    intent.status = payload.outcome
    intent.platform_message_id = payload.platform_message_id
    intent.safe_error_code = payload.safe_error_code
    intent.completed_at = now
    attempt = RenderDeliveryAttempt(
        artifact_id=plan.artifact_id,
        instance_id=authenticated_instance,
        plan_id=plan.id,
        intent_id=intent.id,
        outcome=payload.outcome,
        platform_message_id=payload.platform_message_id,
        safe_error_code=payload.safe_error_code,
        created_at=now,
    )
    session.add(attempt)
    await session.commit()
    return intent, attempt, False
