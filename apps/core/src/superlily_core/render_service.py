"""Core-owned render orchestration, artifact publication, and delivery evidence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from time import monotonic
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import DeliveryAttemptIn, RenderDocument, render_document_hash
from .artifact_store import ArtifactStore, ArtifactStoreError
from .document_renderer_client import DocumentRendererClient, DocumentRendererError
from .models import (
    RenderArtifactRecord,
    RenderDeliveryAttempt,
    RenderDocumentRecord,
    utc_now,
)
from .settings import Settings


class RenderServiceError(RuntimeError):
    def __init__(self, code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _one_chunk(content: bytes) -> AsyncIterator[bytes]:
    yield content


async def submit_render_document(
    session: AsyncSession,
    settings: Settings,
    document: RenderDocument,
    idempotency_key: str,
) -> tuple[RenderDocumentRecord, RenderArtifactRecord, bool]:
    request_sha256 = render_document_hash(document)
    existing = await session.scalar(
        select(RenderDocumentRecord).where(
            RenderDocumentRecord.instance_id == document.instance_id,
            RenderDocumentRecord.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_sha256 != request_sha256:
            raise RenderServiceError("idempotency_conflict", "idempotency key was reused")
        if existing.status != "succeeded":
            raise RenderServiceError(
                existing.safe_error_code or "render_in_progress",
                "render request is not available",
            )
        artifact = await session.scalar(
            select(RenderArtifactRecord).where(RenderArtifactRecord.render_id == existing.id)
        )
        if artifact is None:
            raise RenderServiceError("artifact_missing", "render artifact is unavailable")
        return existing, artifact, True

    if not settings.render_enabled:
        raise RenderServiceError("render_disabled", "document rendering is disabled")
    if document.conversation_key not in settings.render_canary_conversations:
        raise RenderServiceError("conversation_not_canary", "conversation is not in the render canary")

    record = RenderDocumentRecord(
        instance_id=document.instance_id,
        conversation_key=document.conversation_key,
        source_event_id=document.source_event_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        document_json=document.model_dump(mode="json"),
    )
    session.add(record)
    await session.commit()
    render_started = monotonic()
    try:
        worker = DocumentRendererClient(
            settings.render_backend_url,
            settings.render_backend_token,
        )
        result = await worker.render_document(
            document,
            timeout_seconds=float(settings.render_timeout_seconds),
        )
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
        now = utc_now()
        artifact = RenderArtifactRecord(
            id=artifact_id,
            render_id=record.id,
            content_sha256=upload.content_sha256,
            storage_key=storage_key,
            mime_type=upload.mime_type,
            byte_size=upload.byte_size,
            width_pixels=upload.width_pixels,
            height_pixels=upload.height_pixels,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.render_artifact_ttl_seconds),
        )
        record.status = "succeeded"
        record.render_duration_ms = min(120_000, round((monotonic() - render_started) * 1_000))
        record.completed_at = now
        session.add(artifact)
        await session.commit()
        return record, artifact, False
    except RenderServiceError:
        record.status = "failed"
        record.render_duration_ms = min(120_000, round((monotonic() - render_started) * 1_000))
        record.safe_error_code = "renderer_integrity_failure"
        record.completed_at = utc_now()
        await session.commit()
        raise
    except DocumentRendererError as exc:
        record.status = "failed"
        record.render_duration_ms = min(120_000, round((monotonic() - render_started) * 1_000))
        record.safe_error_code = f"renderer_{exc.error_code}"
        record.completed_at = utc_now()
        await session.commit()
        raise RenderServiceError(record.safe_error_code, "document renderer failed safely") from exc
    except ArtifactStoreError as exc:
        record.status = "failed"
        record.render_duration_ms = min(120_000, round((monotonic() - render_started) * 1_000))
        record.safe_error_code = "artifact_store_failure"
        record.completed_at = utc_now()
        await session.commit()
        raise RenderServiceError(record.safe_error_code, "render artifact could not be published") from exc


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
    content = await ArtifactStore(settings.artifact_root).read_object(
        artifact.storage_key,
        byte_size=artifact.byte_size,
        content_sha256=artifact.content_sha256,
    )
    return artifact, content


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
        outcome=payload.outcome,
        platform_message_id=payload.platform_message_id,
        safe_error_code=payload.safe_error_code,
    )
    session.add(attempt)
    await session.commit()
    return attempt
