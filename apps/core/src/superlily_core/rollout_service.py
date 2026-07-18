"""M3 Git-bound rollout plan 导入、精确匹配与原子调用配额。"""

from __future__ import annotations

from datetime import datetime, timezone
import re

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import ToolRolloutPlan, load_tool_rollout_plan

from .models import (
    ToolRolloutPlanCounter,
    ToolRolloutPlanItemRecord,
    ToolRolloutPlanLifecycleEvent,
    ToolRolloutPlanRecord,
)


_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


def _exact_text(value: str, *, label: str) -> str:
    if value != value.strip() or not 1 <= len(value) <= 256:
        raise _unprocessable(f"{label} must be exact and between 1 and 256 characters")
    return value


def _same_import(
    record: ToolRolloutPlanRecord,
    *,
    plan: ToolRolloutPlan,
    plan_hash: str,
    canonical_json: bytes,
    source_commit: str,
    bundle_hash: str,
    reviewer: str,
) -> bool:
    return (
        record.plan_id == plan.plan_id
        and record.version == plan.version
        and record.plan_hash == plan_hash
        and record.canonical_json == canonical_json
        and record.source_commit == source_commit
        and record.bundle_hash == bundle_hash
        and record.reviewer == reviewer
    )


async def import_tool_rollout_plan(
    session: AsyncSession,
    source: bytes,
    *,
    source_commit: str,
    bundle_hash: str,
    reviewer: str,
) -> tuple[ToolRolloutPlanRecord, bool]:
    """只从已核对的 Git 对象导入 reviewed plan，不自动激活。"""

    if not _GIT_COMMIT_RE.fullmatch(source_commit):
        raise _unprocessable("source_commit must be a full lowercase Git object ID")
    if not _SHA256_RE.fullmatch(bundle_hash):
        raise _unprocessable("bundle_hash must be a lowercase SHA-256")
    reviewer = _exact_text(reviewer, label="reviewer")
    try:
        loaded = load_tool_rollout_plan(source)
    except ValueError as exc:
        raise _unprocessable("rollout plan authority validation failed") from exc
    if bundle_hash != loaded.authority.sha256:
        raise _unprocessable("single-plan bundle_hash must equal the canonical plan hash")

    plan = loaded.plan
    existing_identity = await session.scalar(
        select(ToolRolloutPlanRecord).where(
            ToolRolloutPlanRecord.plan_id == plan.plan_id,
            ToolRolloutPlanRecord.version == plan.version,
        )
    )
    existing_hash = await session.scalar(
        select(ToolRolloutPlanRecord).where(
            ToolRolloutPlanRecord.plan_hash == loaded.authority.sha256
        )
    )
    existing = existing_identity or existing_hash
    if existing is not None:
        if _same_import(
            existing,
            plan=plan,
            plan_hash=loaded.authority.sha256,
            canonical_json=loaded.authority.canonical_bytes,
            source_commit=source_commit,
            bundle_hash=bundle_hash,
            reviewer=reviewer,
        ):
            return existing, True
        raise _conflict("rollout plan identity or hash has different authority metadata")

    record = ToolRolloutPlanRecord(
        plan_id=plan.plan_id,
        version=plan.version,
        plan_hash=loaded.authority.sha256,
        schema_version=plan.schema_version,
        mode=plan.mode,
        rollback_mode=plan.rollback_mode,
        review_status="reviewed",
        lifecycle="reviewed",
        resource_version=1,
        starts_at=plan.starts_at,
        expires_at=plan.expires_at,
        max_invocations=plan.max_invocations,
        reason=plan.reason,
        source_commit=source_commit,
        bundle_hash=bundle_hash,
        reviewer=reviewer,
        canonical_json=loaded.authority.canonical_bytes,
        plan_json=plan.model_dump(mode="json"),
        import_outcome="accepted",
    )
    session.add(record)
    try:
        await session.flush()
        session.add_all(
            [
                ToolRolloutPlanItemRecord(
                    plan_record_id=record.id,
                    **item.model_dump(mode="json"),
                )
                for item in plan.items
            ]
        )
        session.add(
            ToolRolloutPlanLifecycleEvent(
                plan_record_id=record.id,
                sequence=1,
                previous_lifecycle=None,
                lifecycle="reviewed",
                actor=reviewer,
                reason="Git-reviewed rollout plan imported with execution disabled",
            )
        )
        session.add(
            ToolRolloutPlanCounter(
                plan_record_id=record.id,
                consumed_invocations=0,
                last_consumed_at=None,
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.scalar(
            select(ToolRolloutPlanRecord).where(
                ToolRolloutPlanRecord.plan_id == plan.plan_id,
                ToolRolloutPlanRecord.version == plan.version,
                ToolRolloutPlanRecord.plan_hash == loaded.authority.sha256,
            )
        )
        if duplicate is not None and _same_import(
            duplicate,
            plan=plan,
            plan_hash=loaded.authority.sha256,
            canonical_json=loaded.authority.canonical_bytes,
            source_commit=source_commit,
            bundle_hash=bundle_hash,
            reviewer=reviewer,
        ):
            return duplicate, True
        raise _conflict("concurrent rollout plan import conflicts with stored authority") from exc
    await session.refresh(record)
    return record, False


async def matching_active_rollout(
    session: AsyncSession,
    *,
    database_time: datetime,
    tool_id: str,
    descriptor_version: str,
    descriptor_hash: str,
    canonical_conversation: str,
    caller: str,
) -> tuple[ToolRolloutPlanRecord, ToolRolloutPlanItemRecord] | None:
    """从数据库 authority 精确选择当前生效的唯一 plan/item。"""

    row = (
        await session.execute(
            select(ToolRolloutPlanRecord, ToolRolloutPlanItemRecord)
            .join(
                ToolRolloutPlanItemRecord,
                ToolRolloutPlanItemRecord.plan_record_id == ToolRolloutPlanRecord.id,
            )
            .where(
                ToolRolloutPlanRecord.lifecycle == "active",
                ToolRolloutPlanRecord.mode == "canary",
                ToolRolloutPlanRecord.starts_at <= database_time,
                ToolRolloutPlanRecord.expires_at > database_time,
                ToolRolloutPlanItemRecord.tool_id == tool_id,
                ToolRolloutPlanItemRecord.descriptor_version == descriptor_version,
                ToolRolloutPlanItemRecord.descriptor_hash == descriptor_hash,
                ToolRolloutPlanItemRecord.canonical_conversation == canonical_conversation,
                ToolRolloutPlanItemRecord.caller == caller,
            )
            .with_for_update(of=ToolRolloutPlanRecord)
        )
    ).one_or_none()
    if row is None:
        return None
    return row[0], row[1]


async def consume_rollout_invocation(
    session: AsyncSession,
    plan: ToolRolloutPlanRecord,
    *,
    database_time: datetime,
) -> bool:
    """在同一事务中原子消费一个调用额度；并发最多一个胜者。"""

    result = await session.execute(
        update(ToolRolloutPlanCounter)
        .where(
            ToolRolloutPlanCounter.plan_record_id == plan.id,
            ToolRolloutPlanCounter.consumed_invocations < plan.max_invocations,
        )
        .values(
            consumed_invocations=ToolRolloutPlanCounter.consumed_invocations + 1,
            last_consumed_at=database_time,
        )
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def locked_rollout_for_lease(
    session: AsyncSession,
    *,
    plan_record_id: str,
    plan_item_id: str,
    database_time: datetime,
) -> tuple[ToolRolloutPlanRecord, ToolRolloutPlanItemRecord] | None:
    """与 pause 共享 plan 行锁，并重新验证窗口和精确 item。"""

    plan = await session.scalar(
        select(ToolRolloutPlanRecord)
        .where(ToolRolloutPlanRecord.id == plan_record_id)
        .with_for_update()
    )
    if (
        plan is None
        or plan.lifecycle != "active"
        or plan.mode != "canary"
        or _aware(plan.starts_at) > database_time
        or _aware(plan.expires_at) <= database_time
    ):
        return None
    item = await session.scalar(
        select(ToolRolloutPlanItemRecord).where(
            ToolRolloutPlanItemRecord.id == plan_item_id,
            ToolRolloutPlanItemRecord.plan_record_id == plan.id,
        )
    )
    if item is None:
        return None
    return plan, item
