from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import QQDirectorySnapshotIn, qq_directory_snapshot_hash

from .models import (
    ConversationNameObservation,
    IdentityNameObservation,
    QQDirectorySnapshot,
    QQFriendSnapshot,
    QQGroupMemberSnapshot,
)
from .service import ensure_instance
from .settings import Settings


def _name_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, ":".join(("superlily-name", kind, *parts))))


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


async def _record_identity_name(
    session: AsyncSession,
    *,
    record: QQDirectorySnapshot,
    user_id: str,
    name_kind: str,
    name_value: str | None,
    conversation_id: str | None,
) -> None:
    value = _text(name_value)
    if value is None:
        return
    filters = [
        IdentityNameObservation.platform == "qq",
        IdentityNameObservation.user_id == user_id,
        IdentityNameObservation.name_kind == name_kind,
        IdentityNameObservation.instance_id == record.instance_id,
    ]
    if name_kind != "account_name":
        filters.extend(
            (
                IdentityNameObservation.conversation_type == "group",
                IdentityNameObservation.conversation_id == conversation_id,
            )
        )
    latest = await session.scalar(
        select(IdentityNameObservation)
        .where(*filters)
        .order_by(
            IdentityNameObservation.observed_at.desc(),
            IdentityNameObservation.recorded_at.desc(),
            IdentityNameObservation.id.desc(),
        )
        .limit(1)
    )
    if latest is not None and latest.name_value == value:
        return
    source_record_id = f"{record.id}:{user_id}"
    session.add(
        IdentityNameObservation(
            id=_name_id("identity", "superlily_core", "qq_directory_snapshot", source_record_id, name_kind),
            platform="qq",
            user_id=user_id,
            conversation_type="group" if conversation_id is not None else None,
            conversation_id=conversation_id,
            name_kind=name_kind,
            name_value=value,
            observed_at=record.observed_at,
            instance_id=record.instance_id,
            source_system="superlily_core",
            source_record_type="qq_directory_snapshot",
            source_record_id=source_record_id,
            observation_method="onebot_directory_snapshot",
            provenance_json={"snapshot_id": record.snapshot_id, "snapshot_kind": record.snapshot_kind},
        )
    )


async def _record_group_name(session: AsyncSession, record: QQDirectorySnapshot) -> None:
    value = _text(record.group_name)
    if record.group_id is None or value is None:
        return
    latest = await session.scalar(
        select(ConversationNameObservation)
        .where(
            ConversationNameObservation.platform == "qq",
            ConversationNameObservation.conversation_type == "group",
            ConversationNameObservation.conversation_id == record.group_id,
            ConversationNameObservation.instance_id == record.instance_id,
        )
        .order_by(
            ConversationNameObservation.observed_at.desc(),
            ConversationNameObservation.recorded_at.desc(),
            ConversationNameObservation.id.desc(),
        )
        .limit(1)
    )
    if latest is not None and latest.name_value == value:
        return
    session.add(
        ConversationNameObservation(
            id=_name_id("conversation", "superlily_core", "qq_directory_snapshot", record.id),
            platform="qq",
            conversation_type="group",
            conversation_id=record.group_id,
            name_value=value,
            observed_at=record.observed_at,
            instance_id=record.instance_id,
            source_system="superlily_core",
            source_record_type="qq_directory_snapshot",
            source_record_id=record.id,
            observation_method="onebot_directory_snapshot",
            provenance_json={"snapshot_id": record.snapshot_id},
        )
    )


async def ingest_qq_directory_snapshot(
    session: AsyncSession,
    payload: QQDirectorySnapshotIn,
    settings: Settings,
) -> tuple[QQDirectorySnapshot, bool]:
    group = payload.group.model_dump(mode="json") if payload.group is not None else None
    members = [item.model_dump(mode="json") for item in payload.members]
    friends = [item.model_dump(mode="json") for item in payload.friends]
    expected_hash = qq_directory_snapshot_hash(
        snapshot_kind=payload.snapshot_kind,
        group=group,
        members=members,
        friends=friends,
        source_apis=payload.source_apis,
        capture_status=payload.capture_status,
        reason=payload.reason,
    )
    if expected_hash != payload.snapshot_hash:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="snapshot_hash does not match directory snapshot content",
        )
    existing = await session.scalar(
        select(QQDirectorySnapshot).where(
            QQDirectorySnapshot.instance_id == payload.instance.instance_id,
            QQDirectorySnapshot.snapshot_id == payload.snapshot_id,
        )
    )
    if existing is not None:
        if existing.snapshot_hash != payload.snapshot_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="snapshot_id was already used for different content",
            )
        return existing, True

    await ensure_instance(session, payload.instance, settings)
    record = QQDirectorySnapshot(
        instance_id=payload.instance.instance_id,
        snapshot_id=payload.snapshot_id,
        snapshot_hash=payload.snapshot_hash,
        snapshot_kind=payload.snapshot_kind,
        group_id=payload.group.group_id if payload.group is not None else None,
        group_name=payload.group.group_name if payload.group is not None else None,
        group_remark=payload.group.group_remark if payload.group is not None else None,
        member_count=payload.group.member_count if payload.group is not None else None,
        max_member_count=payload.group.max_member_count if payload.group is not None else None,
        whole_group_ban=payload.group.whole_group_ban if payload.group is not None else None,
        observed_at=payload.observed_at,
        source_apis_json=sorted(payload.source_apis),
        entry_count=len(members) if payload.snapshot_kind == "group" else len(friends),
        capture_status=payload.capture_status,
        reason=payload.reason,
    )
    session.add(record)
    await session.flush()
    for item in payload.members:
        session.add(QQGroupMemberSnapshot(snapshot_record_id=record.id, **item.model_dump()))
        await _record_identity_name(
            session,
            record=record,
            user_id=item.user_id,
            name_kind="account_name",
            name_value=item.nickname,
            conversation_id=record.group_id,
        )
        await _record_identity_name(
            session,
            record=record,
            user_id=item.user_id,
            name_kind="conversation_display_name",
            name_value=item.card,
            conversation_id=record.group_id,
        )
    for item in payload.friends:
        session.add(QQFriendSnapshot(snapshot_record_id=record.id, **item.model_dump()))
        await _record_identity_name(
            session,
            record=record,
            user_id=item.user_id,
            name_kind="account_name",
            name_value=item.nickname,
            conversation_id=None,
        )
    await _record_group_name(session, record)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        duplicate = await session.scalar(
            select(QQDirectorySnapshot).where(
                QQDirectorySnapshot.instance_id == payload.instance.instance_id,
                QQDirectorySnapshot.snapshot_id == payload.snapshot_id,
            )
        )
        if duplicate is not None and duplicate.snapshot_hash == payload.snapshot_hash:
            return duplicate, True
        raise
    await session.refresh(record)
    return record, False
