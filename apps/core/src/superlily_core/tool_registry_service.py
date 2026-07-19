"""Phase 3a Tool Registry persistence and effective-state evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    ProviderHeartbeatIn,
    ProviderInventorySnapshotIn,
    ProviderRegistration,
    ToolDescriptor,
    load_tool_descriptor,
)

from .models import (
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    ToolProvider,
    ToolProviderCredential,
    ToolProviderHeartbeat,
    ToolProviderInventoryEntry,
    ToolProviderInventorySnapshot,
    ToolProviderLifecycleEvent,
    ToolRolloutPlanCounter,
    ToolRolloutPlanRecord,
    utc_now,
)
from .settings import Settings


_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_ORDER = (
    "not_reviewed",
    "inactive_descriptor",
    "tool_suspended",
    "artifact_storage_unavailable",
    "artifact_mime_unsupported",
    "provider_missing",
    "provider_quarantined",
    "inventory_missing",
    "inventory_stale",
    "inventory_hash_mismatch",
    "descriptor_missing",
    "descriptor_mismatch",
    "implementation_mismatch",
    "protocol_incompatible",
    "budget_unenforceable",
    "provider_stale",
    "provider_unhealthy",
    "caller_forbidden",
    "principal_unauthorized",
    "capability_unavailable",
    "execution_off",
    "global_stop",
)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _exact_text(value: str, *, label: str, max_length: int = 256) -> str:
    if value != value.strip() or not value or len(value) > max_length:
        raise _unprocessable(
            f"{label} must be an exact non-empty string of at most {max_length} characters"
        )
    return value


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _age_seconds(value: datetime, now: datetime) -> int:
    return max(0, int((now - _aware(value)).total_seconds()))


def _ordered_reasons(reasons: set[str]) -> list[str]:
    return [reason for reason in _REASON_ORDER if reason in reasons]


def _same_descriptor_import(
    record: ToolDescriptorRecord,
    *,
    tool_id: str,
    version: str,
    descriptor_hash: str,
    canonical_json: bytes,
    source_commit: str,
    bundle_hash: str,
    reviewer: str,
) -> bool:
    return (
        record.tool_id == tool_id
        and record.version == version
        and record.descriptor_hash == descriptor_hash
        and record.canonical_json == canonical_json
        and record.source_commit == source_commit
        and record.bundle_hash == bundle_hash
        and record.reviewer == reviewer
    )


def _same_provider_registration(
    record: ToolProvider,
    registration: ProviderRegistration,
    *,
    initial_lifecycle: str | None,
) -> bool:
    return {
        "provider_id": record.id,
        "owner": record.owner,
        "lifecycle": initial_lifecycle,
        "allowed_protocols": record.allowed_protocols_json,
        "tool_selectors": record.tool_selectors_json,
    } == registration.model_dump(mode="json")


async def _initial_provider_lifecycle(
    session: AsyncSession,
    provider_id: str,
) -> str | None:
    return await session.scalar(
        select(ToolProviderLifecycleEvent.lifecycle).where(
            ToolProviderLifecycleEvent.provider_id == provider_id,
            ToolProviderLifecycleEvent.sequence == 1,
            ToolProviderLifecycleEvent.previous_lifecycle.is_(None),
        )
    )


def _same_inventory(
    record: ToolProviderInventorySnapshot,
    payload: ProviderInventorySnapshotIn,
) -> bool:
    return (
        record.snapshot_hash == payload.snapshot_hash
        and _aware(record.observed_at) == _aware(payload.observed_at)
        and record.protocol_version == payload.protocol_version
    )


def _same_heartbeat(record: ToolProviderHeartbeat, payload: ProviderHeartbeatIn) -> bool:
    return (
        record.inventory_hash == payload.inventory_hash
        and record.health == payload.health
        and record.current_concurrency == payload.current_concurrency
        and record.max_concurrency == payload.max_concurrency
        and record.oldest_work_age_ms == payload.oldest_work_age_ms
        and record.metadata_json == payload.metadata
    )


async def import_tool_descriptor(
    session: AsyncSession,
    source: bytes,
    *,
    source_commit: str,
    bundle_hash: str,
    reviewer: str,
) -> tuple[ToolDescriptorRecord, bool]:
    """Import one reviewed Git authority document without activating it."""

    if not _GIT_COMMIT_RE.fullmatch(source_commit):
        raise _unprocessable("source_commit must be a lowercase 40-to-64 character Git object ID")
    if not _SHA256_RE.fullmatch(bundle_hash):
        raise _unprocessable("bundle_hash must be a lowercase SHA-256")
    reviewer = _exact_text(reviewer, label="reviewer")
    try:
        loaded = load_tool_descriptor(source)
    except ValueError as exc:
        raise _unprocessable("descriptor authority validation failed") from exc
    if bundle_hash != loaded.authority.sha256:
        raise _unprocessable(
            "Phase 3a single-descriptor bundle_hash must equal the canonical descriptor hash"
        )

    descriptor = loaded.descriptor
    existing_identity = await session.scalar(
        select(ToolDescriptorRecord).where(
            ToolDescriptorRecord.tool_id == descriptor.tool_id,
            ToolDescriptorRecord.version == descriptor.version,
        )
    )
    existing_hash = await session.scalar(
        select(ToolDescriptorRecord).where(
            ToolDescriptorRecord.descriptor_hash == loaded.authority.sha256
        )
    )
    existing = existing_identity or existing_hash
    if existing is not None:
        if _same_descriptor_import(
            existing,
            tool_id=descriptor.tool_id,
            version=descriptor.version,
            descriptor_hash=loaded.authority.sha256,
            canonical_json=loaded.authority.canonical_bytes,
            source_commit=source_commit,
            bundle_hash=bundle_hash,
            reviewer=reviewer,
        ):
            return existing, True
        raise _conflict("tool descriptor identity or hash already has different authority metadata")

    record = ToolDescriptorRecord(
        tool_id=descriptor.tool_id,
        version=descriptor.version,
        descriptor_hash=loaded.authority.sha256,
        schema_profile=descriptor.schema_profile,
        source_plugin=descriptor.source_plugin,
        review_status="reviewed",
        lifecycle="reviewed",
        source_commit=source_commit,
        bundle_hash=bundle_hash,
        reviewer=reviewer,
        canonical_json=loaded.authority.canonical_bytes,
        descriptor_json=descriptor.model_dump(mode="json"),
        import_outcome="accepted",
    )
    session.add(record)
    try:
        await session.flush()
        session.add(
            ToolDescriptorLifecycleEvent(
                descriptor_id=record.id,
                sequence=1,
                previous_lifecycle=None,
                lifecycle="reviewed",
                actor=reviewer,
                reason="Git-reviewed descriptor imported with execution disabled",
            )
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.scalar(
            select(ToolDescriptorRecord).where(
                ToolDescriptorRecord.tool_id == descriptor.tool_id,
                ToolDescriptorRecord.version == descriptor.version,
                ToolDescriptorRecord.descriptor_hash == loaded.authority.sha256,
            )
        )
        if duplicate is not None and _same_descriptor_import(
            duplicate,
            tool_id=descriptor.tool_id,
            version=descriptor.version,
            descriptor_hash=loaded.authority.sha256,
            canonical_json=loaded.authority.canonical_bytes,
            source_commit=source_commit,
            bundle_hash=bundle_hash,
            reviewer=reviewer,
        ):
            return duplicate, True
        raise _conflict("concurrent tool descriptor import conflicts with stored authority") from exc
    await session.refresh(record)
    return record, False


async def register_tool_provider(
    session: AsyncSession,
    registration: ProviderRegistration,
    *,
    actor: str,
    settings: Settings,
) -> tuple[ToolProvider, bool]:
    """Create one stable provider and an environment-backed credential reference."""

    actor = _exact_text(actor, label="actor")
    if registration.provider_id not in settings.provider_tokens:
        raise _unprocessable("provider has no separately configured provider credential")
    existing = await session.get(ToolProvider, registration.provider_id)
    if existing is not None:
        if _same_provider_registration(
            existing,
            registration,
            initial_lifecycle=await _initial_provider_lifecycle(session, existing.id),
        ):
            return existing, True
        raise _conflict("provider_id already has a different stable registration")

    record = ToolProvider(
        id=registration.provider_id,
        owner=registration.owner,
        lifecycle=registration.lifecycle,
        allowed_protocols_json=list(registration.allowed_protocols),
        tool_selectors_json=list(registration.tool_selectors),
    )
    session.add(record)
    try:
        await session.flush()
        session.add_all(
            [
                ToolProviderCredential(
                    id=registration.provider_id,
                    provider_id=registration.provider_id,
                    source="environment",
                    lifecycle="active",
                ),
                ToolProviderLifecycleEvent(
                    provider_id=registration.provider_id,
                    sequence=1,
                    previous_lifecycle=None,
                    lifecycle=registration.lifecycle,
                    actor=actor,
                    reason="Stable provider registration created",
                ),
            ]
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.get(ToolProvider, registration.provider_id)
        if duplicate is not None and _same_provider_registration(
            duplicate,
            registration,
            initial_lifecycle=await _initial_provider_lifecycle(session, duplicate.id),
        ):
            return duplicate, True
        raise _conflict("concurrent provider registration conflict") from exc
    await session.refresh(record)
    return record, False


async def _authenticated_provider(
    session: AsyncSession,
    provider_id: str,
) -> tuple[ToolProvider, ToolProviderCredential]:
    provider = await session.get(ToolProvider, provider_id)
    credential = await session.get(ToolProviderCredential, provider_id)
    if provider is None or credential is None or credential.lifecycle != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="provider credential is not bound to an active registration",
        )
    if provider.lifecycle in {"retired", "revoked"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="provider registration no longer accepts runtime reports",
        )
    credential.last_authenticated_at = utc_now()
    return provider, credential


async def ingest_provider_inventory(
    session: AsyncSession,
    payload: ProviderInventorySnapshotIn,
    idempotency_key: str,
) -> tuple[ToolProviderInventorySnapshot, bool]:
    """Store one immutable authenticated provider inventory observation."""

    idempotency_key = _exact_text(
        idempotency_key,
        label="Idempotency-Key",
        max_length=256,
    )
    provider, _ = await _authenticated_provider(session, payload.provider_id)
    if payload.protocol_version not in provider.allowed_protocols_json:
        raise _unprocessable("provider inventory protocol is not authorized by registration")
    existing = await session.scalar(
        select(ToolProviderInventorySnapshot).where(
            ToolProviderInventorySnapshot.provider_id == payload.provider_id,
            ToolProviderInventorySnapshot.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if not _same_inventory(existing, payload):
            raise _conflict("inventory idempotency key was reused with different content")
        await session.commit()
        return existing, True

    record = ToolProviderInventorySnapshot(
        provider_id=payload.provider_id,
        idempotency_key=idempotency_key,
        snapshot_hash=payload.snapshot_hash,
        observed_at=payload.observed_at,
        protocol_version=payload.protocol_version,
    )
    session.add(record)
    try:
        await session.flush()
        session.add_all(
            [
                ToolProviderInventoryEntry(
                    snapshot_id=record.id,
                    tool_id=item.tool_id,
                    descriptor_version=item.descriptor_version,
                    descriptor_hash=item.descriptor_hash,
                    protocol_version=item.protocol_version,
                    implementation_hash=item.implementation_hash,
                    budget_enforcement_json=item.budget_enforcement,
                )
                for item in payload.tools
            ]
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.scalar(
            select(ToolProviderInventorySnapshot).where(
                ToolProviderInventorySnapshot.provider_id == payload.provider_id,
                ToolProviderInventorySnapshot.idempotency_key == idempotency_key,
            )
        )
        if duplicate is not None and _same_inventory(duplicate, payload):
            return duplicate, True
        raise _conflict("concurrent provider inventory conflict") from exc
    await session.refresh(record)
    return record, False


async def ingest_provider_heartbeat(
    session: AsyncSession,
    payload: ProviderHeartbeatIn,
) -> tuple[ToolProviderHeartbeat, bool]:
    """Append provider health tied to a previously accepted inventory hash."""

    await _authenticated_provider(session, payload.provider_id)
    inventory = await session.scalar(
        select(ToolProviderInventorySnapshot).where(
            ToolProviderInventorySnapshot.provider_id == payload.provider_id,
            ToolProviderInventorySnapshot.snapshot_hash == payload.inventory_hash,
        )
    )
    if inventory is None:
        raise _unprocessable("provider heartbeat references an unknown inventory hash")
    existing = await session.scalar(
        select(ToolProviderHeartbeat).where(
            ToolProviderHeartbeat.provider_id == payload.provider_id,
            ToolProviderHeartbeat.observed_at == payload.observed_at,
        )
    )
    if existing is not None:
        if not _same_heartbeat(existing, payload):
            raise _conflict("provider heartbeat timestamp was reused with different content")
        await session.commit()
        return existing, True

    record = ToolProviderHeartbeat(
        provider_id=payload.provider_id,
        inventory_hash=payload.inventory_hash,
        observed_at=payload.observed_at,
        health=payload.health,
        current_concurrency=payload.current_concurrency,
        max_concurrency=payload.max_concurrency,
        oldest_work_age_ms=payload.oldest_work_age_ms,
        metadata_json=payload.metadata,
    )
    session.add(record)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await session.scalar(
            select(ToolProviderHeartbeat).where(
                ToolProviderHeartbeat.provider_id == payload.provider_id,
                ToolProviderHeartbeat.observed_at == payload.observed_at,
            )
        )
        if duplicate is not None and _same_heartbeat(duplicate, payload):
            return duplicate, True
        raise _conflict("concurrent provider heartbeat conflict") from exc
    await session.refresh(record)
    return record, False


def _provider_tool_reasons(
    descriptor: ToolDescriptor,
    descriptor_record: ToolDescriptorRecord,
    provider: ToolProvider | None,
    inventory: ToolProviderInventorySnapshot | None,
    entries: list[ToolProviderInventoryEntry],
    heartbeat: ToolProviderHeartbeat | None,
    *,
    inventory_age: int | None,
    heartbeat_age: int | None,
    settings: Settings,
) -> set[str]:
    reasons: set[str] = set()
    if provider is None or provider.lifecycle == "registered":
        return {"provider_missing"}
    if provider.lifecycle in {"quarantined", "retired", "revoked"}:
        reasons.add("provider_quarantined")
    if descriptor.tool_id not in provider.tool_selectors_json:
        reasons.add("descriptor_missing")
    if descriptor.provider_selector.protocol not in provider.allowed_protocols_json:
        reasons.add("protocol_incompatible")
    if inventory is None:
        reasons.add("inventory_missing")
    else:
        if inventory_age is not None and inventory_age > settings.provider_inventory_stale_seconds:
            reasons.add("inventory_stale")
        if inventory.protocol_version != descriptor.provider_selector.protocol:
            reasons.add("protocol_incompatible")
        entry = next((item for item in entries if item.tool_id == descriptor.tool_id), None)
        if entry is None:
            reasons.add("descriptor_missing")
        else:
            if (
                entry.descriptor_version != descriptor.version
                or entry.descriptor_hash != descriptor_record.descriptor_hash
            ):
                reasons.add("descriptor_mismatch")
            if entry.protocol_version != descriptor.provider_selector.protocol:
                reasons.add("protocol_incompatible")
            if any(
                entry.budget_enforcement_json.get(name) != "hard"
                for name in descriptor.required_budget_enforcement
            ):
                reasons.add("budget_unenforceable")
    if heartbeat is None:
        reasons.add("provider_stale")
    else:
        if heartbeat_age is not None and heartbeat_age > settings.provider_heartbeat_stale_seconds:
            reasons.add("provider_stale")
        if heartbeat.health != "healthy":
            reasons.add("provider_unhealthy")
        if inventory is not None and heartbeat.inventory_hash != inventory.snapshot_hash:
            reasons.add("inventory_hash_mismatch")
    return reasons


async def tool_registry_view(
    session: AsyncSession,
    settings: Settings,
    *,
    tool_id: str | None = None,
) -> dict[str, Any]:
    """返回 desired、reported 与当前只记账执行模式下的 effective 状态。"""

    descriptor_statement = select(ToolDescriptorRecord).order_by(
        ToolDescriptorRecord.tool_id, ToolDescriptorRecord.version
    )
    if tool_id is not None:
        descriptor_statement = descriptor_statement.where(ToolDescriptorRecord.tool_id == tool_id)
    descriptors = (await session.scalars(descriptor_statement)).all()
    providers = (await session.scalars(select(ToolProvider).order_by(ToolProvider.id))).all()
    credentials = (
        await session.scalars(select(ToolProviderCredential).order_by(ToolProviderCredential.id))
    ).all()
    latest_inventory_sequence = (
        select(
            ToolProviderInventorySnapshot.provider_id,
            func.max(ToolProviderInventorySnapshot.sequence).label("sequence"),
        )
        .group_by(ToolProviderInventorySnapshot.provider_id)
        .subquery()
    )
    inventory_rows = (
        await session.scalars(
            select(ToolProviderInventorySnapshot).join(
                latest_inventory_sequence,
                (ToolProviderInventorySnapshot.provider_id == latest_inventory_sequence.c.provider_id)
                & (ToolProviderInventorySnapshot.sequence == latest_inventory_sequence.c.sequence),
            )
        )
    ).all()
    latest_heartbeat_sequence = (
        select(
            ToolProviderHeartbeat.provider_id,
            func.max(ToolProviderHeartbeat.sequence).label("sequence"),
        )
        .group_by(ToolProviderHeartbeat.provider_id)
        .subquery()
    )
    heartbeat_rows = (
        await session.scalars(
            select(ToolProviderHeartbeat).join(
                latest_heartbeat_sequence,
                (ToolProviderHeartbeat.provider_id == latest_heartbeat_sequence.c.provider_id)
                & (ToolProviderHeartbeat.sequence == latest_heartbeat_sequence.c.sequence),
            )
        )
    ).all()
    latest_inventory = {row.provider_id: row for row in inventory_rows}
    latest_heartbeat = {row.provider_id: row for row in heartbeat_rows}
    snapshot_ids = [item.id for item in latest_inventory.values()]
    entry_rows = (
        (
            await session.scalars(
                select(ToolProviderInventoryEntry)
                .where(ToolProviderInventoryEntry.snapshot_id.in_(snapshot_ids))
                .order_by(ToolProviderInventoryEntry.tool_id)
            )
        ).all()
        if snapshot_ids
        else []
    )
    entries_by_snapshot: dict[str, list[ToolProviderInventoryEntry]] = {}
    for entry in entry_rows:
        entries_by_snapshot.setdefault(entry.snapshot_id, []).append(entry)
    credential_by_provider = {item.provider_id: item for item in credentials}
    provider_by_id = {item.id: item for item in providers}
    database_now = await session.scalar(select(func.current_timestamp()))
    now = _aware(database_now) if isinstance(database_now, datetime) else datetime.now(timezone.utc)
    rollout_plans = list(
        (
            await session.scalars(
                select(ToolRolloutPlanRecord).order_by(
                    ToolRolloutPlanRecord.plan_id,
                    ToolRolloutPlanRecord.version,
                )
            )
        ).all()
    )
    rollout_counters = {
        item.plan_record_id: item
        for item in (await session.scalars(select(ToolRolloutPlanCounter))).all()
    }
    active_rollout = next(
        (
            item
            for item in rollout_plans
            if item.lifecycle == "active"
            and _aware(item.starts_at) <= now < _aware(item.expires_at)
            and rollout_counters.get(item.id) is not None
            and rollout_counters[item.id].consumed_invocations < item.max_invocations
        ),
        None,
    )

    provider_payloads = []
    for provider in providers:
        inventory = latest_inventory.get(provider.id)
        heartbeat = latest_heartbeat.get(provider.id)
        inventory_age = None if inventory is None else _age_seconds(inventory.received_at, now)
        heartbeat_age = None if heartbeat is None else _age_seconds(heartbeat.received_at, now)
        credential = credential_by_provider.get(provider.id)
        provider_payloads.append(
            {
                "provider_id": provider.id,
                "desired": {
                    "owner": provider.owner,
                    "lifecycle": provider.lifecycle,
                    "allowed_protocols": provider.allowed_protocols_json,
                    "tool_selectors": provider.tool_selectors_json,
                    "credential": (
                        None
                        if credential is None
                        else {
                            "credential_id": credential.id,
                            "source": credential.source,
                            "lifecycle": credential.lifecycle,
                            "last_authenticated_at": credential.last_authenticated_at,
                        }
                    ),
                },
                "reported": {
                    "inventory": (
                        None
                        if inventory is None
                        else {
                            "snapshot_id": inventory.id,
                            "snapshot_hash": inventory.snapshot_hash,
                            "protocol_version": inventory.protocol_version,
                            "observed_at": inventory.observed_at,
                            "received_at": inventory.received_at,
                            "age_seconds": inventory_age,
                            "fresh": inventory_age <= settings.provider_inventory_stale_seconds,
                            "tools": [
                                {
                                    "tool_id": entry.tool_id,
                                    "descriptor_version": entry.descriptor_version,
                                    "descriptor_hash": entry.descriptor_hash,
                                    "implementation_hash": entry.implementation_hash,
                                    "protocol_version": entry.protocol_version,
                                    "budget_enforcement": entry.budget_enforcement_json,
                                }
                                for entry in entries_by_snapshot.get(inventory.id, [])
                            ],
                        }
                    ),
                    "heartbeat": (
                        None
                        if heartbeat is None
                        else {
                            "inventory_hash": heartbeat.inventory_hash,
                            "health": heartbeat.health,
                            "current_concurrency": heartbeat.current_concurrency,
                            "max_concurrency": heartbeat.max_concurrency,
                            "oldest_work_age_ms": heartbeat.oldest_work_age_ms,
                            "metadata": heartbeat.metadata_json,
                            "observed_at": heartbeat.observed_at,
                            "received_at": heartbeat.received_at,
                            "age_seconds": heartbeat_age,
                            "fresh": heartbeat_age <= settings.provider_heartbeat_stale_seconds,
                        }
                    ),
                },
            }
        )

    tool_payloads = []
    for record in descriptors:
        descriptor = ToolDescriptor.model_validate(record.descriptor_json)
        base_reasons: set[str] = set()
        if record.review_status != "reviewed":
            base_reasons.add("not_reviewed")
        if record.lifecycle != "active":
            base_reasons.add("inactive_descriptor")
        if record.lifecycle == "suspended":
            base_reasons.add("tool_suspended")
        if descriptor.execution_permissions.artifacts:
            if not settings.artifact_enabled:
                base_reasons.add("artifact_storage_unavailable")
            if any(
                mime_type != "image/png"
                for mime_type in descriptor.execution_permissions.artifacts
            ):
                base_reasons.add("artifact_mime_unsupported")
        runtime = []
        provider_reason_sets: list[set[str]] = []
        for provider_id in descriptor.provider_selector.provider_ids:
            provider = provider_by_id.get(provider_id)
            inventory = latest_inventory.get(provider_id)
            heartbeat = latest_heartbeat.get(provider_id)
            inventory_age = None if inventory is None else _age_seconds(inventory.received_at, now)
            heartbeat_age = None if heartbeat is None else _age_seconds(heartbeat.received_at, now)
            reasons = _provider_tool_reasons(
                descriptor,
                record,
                provider,
                inventory,
                [] if inventory is None else entries_by_snapshot.get(inventory.id, []),
                heartbeat,
                inventory_age=inventory_age,
                heartbeat_age=heartbeat_age,
                settings=settings,
            )
            inventory_entry = next(
                (
                    item
                    for item in ([] if inventory is None else entries_by_snapshot.get(inventory.id, []))
                    if item.tool_id == descriptor.tool_id
                ),
                None,
            )
            provider_reason_sets.append(reasons)
            runtime.append(
                {
                    "provider_id": provider_id,
                    "inventory_hash": None if inventory is None else inventory.snapshot_hash,
                    "implementation_hash": (
                        None if inventory_entry is None else inventory_entry.implementation_hash
                    ),
                    "budget_enforcement": (
                        None
                        if inventory_entry is None
                        else inventory_entry.budget_enforcement_json
                    ),
                    "heartbeat_health": None if heartbeat is None else heartbeat.health,
                    "reasons": _ordered_reasons(reasons),
                    "runtime_eligible": not reasons,
                }
            )
        reasons = set(base_reasons)
        if provider_reason_sets and not any(not item for item in provider_reason_sets):
            for provider_reasons in provider_reason_sets:
                reasons.update(provider_reasons)
        elif not provider_reason_sets:
            reasons.add("provider_missing")
        if settings.tool_execution_mode == "off":
            reasons.add("execution_off")
        if settings.tool_global_stop:
            reasons.add("global_stop")
        effective_eligible = not reasons
        tool_payloads.append(
            {
                "tool_id": record.tool_id,
                "version": record.version,
                "desired": {
                    "descriptor_hash": record.descriptor_hash,
                    "review_status": record.review_status,
                    "lifecycle": record.lifecycle,
                    "source_commit": record.source_commit,
                    "bundle_hash": record.bundle_hash,
                    "reviewer": record.reviewer,
                    "schema_profile": record.schema_profile,
                    "source_plugin": record.source_plugin,
                    "provider_selector": descriptor.provider_selector.model_dump(mode="json"),
                    "required_budget_enforcement": descriptor.required_budget_enforcement,
                    "allowed_callers": descriptor.allowed_callers,
                    "natural_language": descriptor.natural_language,
                    "imported_at": record.imported_at,
                },
                "reported": runtime,
                "effective": {
                    "eligible": effective_eligible,
                    "execution_mode": settings.tool_execution_mode,
                    "reasons": _ordered_reasons(reasons),
                },
            }
        )

    return {
        "schema_version": "1.0",
        "execution": {
            "mode": settings.tool_execution_mode,
            "global_stop": settings.tool_global_stop,
            "invocation_endpoints": settings.tool_execution_mode != "off",
            "lease_endpoint": True,
            "leases_enabled": settings.tool_execution_mode == "canary"
            and not settings.tool_global_stop
            and active_rollout is not None,
            "natural_language_callers": False,
            "active_rollout_plan": (
                None
                if active_rollout is None
                else {
                    "plan_id": active_rollout.plan_id,
                    "version": active_rollout.version,
                    "plan_hash": active_rollout.plan_hash,
                    "resource_version": active_rollout.resource_version,
                    "starts_at": active_rollout.starts_at,
                    "expires_at": active_rollout.expires_at,
                    "max_invocations": active_rollout.max_invocations,
                    "consumed_invocations": rollout_counters[
                        active_rollout.id
                    ].consumed_invocations,
                }
            ),
        },
        "summary": {
            "descriptors": len(tool_payloads),
            "active_descriptors": sum(
                1 for item in tool_payloads if item["desired"]["lifecycle"] == "active"
            ),
            "eligible_tools": sum(
                1 for item in tool_payloads if item["effective"]["eligible"]
            ),
            "providers": len(provider_payloads),
            "fresh_inventories": sum(
                1
                for item in provider_payloads
                if item["reported"]["inventory"] is not None
                and item["reported"]["inventory"]["fresh"]
            ),
            "healthy_providers": sum(
                1
                for item in provider_payloads
                if item["reported"]["heartbeat"] is not None
                and item["reported"]["heartbeat"]["fresh"]
                and item["reported"]["heartbeat"]["health"] == "healthy"
            ),
            "rollout_plans": len(rollout_plans),
            "active_rollout_plans": sum(
                1 for item in rollout_plans if item.lifecycle == "active"
            ),
        },
        "tools": tool_payloads,
        "providers": provider_payloads,
    }
