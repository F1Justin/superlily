import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import EventIn
from superlily_contracts.media_archive import QQMediaReport

from .models import EventObservation, QQMediaArchiveItem, QQMediaBlob
from .settings import Settings


def report_digest(payload: EventIn) -> str:
    return hashlib.sha256(json.dumps(payload.metadata.get("qq_media_archive"), sort_keys=True,
                                     ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def content_path(settings: Settings, digest: str) -> Path:
    if not settings.qq_media_root:
        raise HTTPException(503, "QQ media content storage disabled")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise HTTPException(422, "invalid content digest")
    root = Path(settings.qq_media_root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise HTTPException(503, "unsafe media storage root")
    if root.exists() and root.stat().st_mode & 0o077:
        raise HTTPException(503, "media storage root must be private")
    return root / digest


def store_content(settings: Settings, digest: str, body: bytes) -> None:
    if len(body) > settings.qq_media_max_bytes:
        raise HTTPException(413, "media exceeds size limit")
    if hashlib.sha256(body).hexdigest() != digest:
        raise HTTPException(422, "media content digest mismatch")
    target = content_path(settings, digest)
    root = target.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_symlink():
            raise HTTPException(503, "unsafe media content")
        if target.exists():
            if target.stat().st_size != len(body) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise HTTPException(503, "media integrity failure")
            return
        used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        if used + len(body) > settings.qq_media_quota_bytes:
            raise HTTPException(507, "media storage quota exceeded")
        fd, name = tempfile.mkstemp(prefix=".part-", dir=root)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, target)
            directory = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)


async def register_content(session: AsyncSession, settings: Settings, instance: str, digest: str, body: bytes):
    from .service import _correlation_guard

    async with _correlation_guard(session, hashlib.sha256(f"qq-blob:{instance}:{digest}".encode()).hexdigest()):
        await asyncio.to_thread(store_content, settings, digest, body)
        blob = await session.get(QQMediaBlob, (instance, digest))
        if blob is None:
            session.add(QQMediaBlob(instance_id=instance, sha256=digest, size_bytes=len(body),
                                    created_at=datetime.now(timezone.utc)))
        elif blob.size_bytes != len(body):
            raise HTTPException(409, "content metadata conflict")
        await session.commit()
    return {"sha256": digest, "size_bytes": len(body), "content_ref": f"/v1/qq-media/blobs/{digest}"}


async def validate_media_report(session: AsyncSession, payload: EventIn, settings: Settings):
    if payload.event_type != "audit.qq_media_archive":
        return None
    try:
        report = QQMediaReport.model_validate(payload.metadata.get("qq_media_archive"))
    except ValidationError as exc:
        raise HTTPException(422, "invalid QQ media archive report") from exc
    for item in report.items:
        if item.sha256:
            blob = await session.get(QQMediaBlob, (payload.instance.instance_id, item.sha256))
            if blob is None or blob.size_bytes != item.size_bytes:
                raise HTTPException(422, "media content has not been verified for this instance")
            path = content_path(settings, item.sha256)
            if not path.is_file() or path.is_symlink() or path.stat().st_size != item.size_bytes:
                raise HTTPException(422, "media content unavailable")
    return report


def record_media_report(session: AsyncSession, payload: EventIn, observation: EventObservation, report: QQMediaReport | None):
    if report is None:
        return
    for item in report.items:
        fields = item.model_dump()
        fields["segments_json"] = fields.pop("segments")
        fields["omitted_fields_json"] = fields.pop("omitted_fields")
        fields["content_ref"] = f"/v1/qq-media/blobs/{item.sha256}" if item.sha256 else None
        session.add(QQMediaArchiveItem(
            observation_id=observation.id, instance_id=payload.instance.instance_id,
            conversation_type=payload.conversation.type, conversation_id=payload.conversation.id,
            parent_source_event_id=report.parent_source_event_id, parent_message_id=report.parent_message_id,
            job_id=report.job_id, revision=report.revision, observed_at=payload.occurred_at, **fields,
        ))
