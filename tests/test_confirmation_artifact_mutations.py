from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import canonicalize_json_value
from superlily_core.models import (
    ToolArtifact,
    ToolArtifactEvent,
    ToolConfirmation,
    ToolConfirmationEvent,
)

from test_tool_execution_api import create_queued, prepare_canary, pull_lease


def _hash(value: object) -> str:
    return canonicalize_json_value(value).sha256


async def _leased_attempt(client, app) -> tuple[dict, dict]:
    descriptor, inventory_hash = await prepare_canary(client, app)
    invocation = await create_queued(
        client, descriptor, key=f"confirmation-artifact-{uuid4()}"
    )
    response = await pull_lease(client, inventory_hash)
    assert response.status_code == 200, response.text
    return invocation, response.json()


async def test_confirmation_current_row_requires_append_only_transition_event(
    client, app
) -> None:
    invocation, _ = await _leased_attempt(client, app)
    now = datetime.now(timezone.utc)
    confirmation_id = str(uuid4())
    request_hash = _hash({"confirmation": confirmation_id})
    evidence = {"request_hash": request_hash, "policy": "always"}
    async with app.state.database.sessions() as session:
        session.add_all(
            [
                ToolConfirmation(
                    id=confirmation_id,
                    invocation_id=invocation["invocation_id"],
                    policy="always",
                    state="pending",
                    resource_version=1,
                    request_hash=request_hash,
                    input_hash="a" * 64,
                    principal_hash="b" * 64,
                    policy_hash="c" * 64,
                    caller_type="admin_api",
                    caller_id="core-admin",
                    required_approvals=1,
                    expires_at=now + timedelta(minutes=2),
                    created_at=now,
                    updated_at=now,
                ),
                ToolConfirmationEvent(
                    confirmation_id=confirmation_id,
                    sequence=1,
                    event="create",
                    previous_state=None,
                    state="pending",
                    actor_type="system",
                    actor_id="confirmation-policy",
                    idempotency_key=f"create:{confirmation_id}",
                    request_hash=request_hash,
                    reason="确认挑战已创建",
                    evidence_json=evidence,
                    evidence_hash=_hash(evidence),
                    effective_at=now,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolConfirmation)
                .where(ToolConfirmation.id == confirmation_id)
                .values(
                    state="consumed",
                    resource_version=2,
                    consumed_at=now + timedelta(seconds=1),
                    updated_at=now + timedelta(seconds=1),
                )
            )
            await session.commit()
        await session.rollback()

    decided_at = now + timedelta(seconds=2)
    decision_evidence = {"decision": "approve", "request_hash": request_hash}
    async with app.state.database.sessions() as session:
        session.add(
            ToolConfirmationEvent(
                confirmation_id=confirmation_id,
                sequence=2,
                event="approve",
                previous_state="pending",
                state="consumed",
                actor_type="admin_api",
                actor_id="core-admin",
                idempotency_key=f"approve:{confirmation_id}",
                request_hash=request_hash,
                reason="用户明确确认执行",
                evidence_json=decision_evidence,
                evidence_hash=_hash(decision_evidence),
                effective_at=decided_at,
                created_at=decided_at,
            )
        )
        await session.flush()
        await session.execute(
            update(ToolConfirmation)
            .where(ToolConfirmation.id == confirmation_id)
            .values(
                state="consumed",
                resource_version=2,
                consumed_at=decided_at,
                updated_at=decided_at,
            )
        )
        await session.commit()

    async with app.state.database.sessions() as session:
        current = await session.get(ToolConfirmation, confirmation_id)
        assert current is not None
        assert current.state == "consumed"
        event = await session.scalar(
            select(ToolConfirmationEvent).where(
                ToolConfirmationEvent.confirmation_id == confirmation_id,
                ToolConfirmationEvent.sequence == 2,
            )
        )
        assert event is not None
        for statement in (
            update(ToolConfirmationEvent)
            .where(ToolConfirmationEvent.id == event.id)
            .values(reason="篡改"),
            delete(ToolConfirmationEvent).where(ToolConfirmationEvent.id == event.id),
            update(ToolConfirmation)
            .where(ToolConfirmation.id == confirmation_id)
            .values(request_hash="d" * 64),
            delete(ToolConfirmation).where(ToolConfirmation.id == confirmation_id),
        ):
            with pytest.raises(DBAPIError):
                await session.execute(statement)
                await session.commit()
            await session.rollback()


async def test_artifact_current_row_requires_fence_bound_append_only_event(
    client, app
) -> None:
    invocation, lease = await _leased_attempt(client, app)
    now = datetime.now(timezone.utc)
    artifact_id = str(uuid4())
    policy = {
        "max_count": 1,
        "max_single_bytes": 1024,
        "max_width_pixels": 64,
        "max_height_pixels": 64,
        "reservation_ttl_seconds": 120,
    }
    reserve_evidence = {"mime_type": "image/png", "policy": policy}
    async with app.state.database.sessions() as session:
        session.add_all(
            [
                ToolArtifact(
                    id=artifact_id,
                    invocation_id=invocation["invocation_id"],
                    attempt_id=lease["attempt_id"],
                    provider_id=lease["provider_id"],
                    fencing_token=lease["fencing_token"],
                    idempotency_key=f"artifact:{artifact_id}",
                    reservation_request_hash=_hash(reserve_evidence),
                    producer_tool_id=lease["tool_id"],
                    producer_descriptor_version=lease["descriptor_version"],
                    producer_descriptor_hash=lease["descriptor_hash"],
                    data_classification="public",
                    canonical_conversation="qq:group:1080353942",
                    state="reserved",
                    resource_version=1,
                    mime_type="image/png",
                    policy_snapshot_json=policy,
                    policy_hash=_hash(policy),
                    max_bytes=1024,
                    max_width_pixels=64,
                    max_height_pixels=64,
                    declared_bytes=None,
                    declared_sha256=None,
                    expires_at=now + timedelta(minutes=2),
                    upload_secret_hash="d" * 64,
                    quarantine_key=f"quarantine/{artifact_id}.part",
                    created_at=now,
                    updated_at=now,
                ),
                ToolArtifactEvent(
                    artifact_id=artifact_id,
                    sequence=1,
                    event="reserve",
                    previous_state=None,
                    state="reserved",
                    actor_type="provider",
                    actor_id=lease["provider_id"],
                    provider_id=lease["provider_id"],
                    fencing_token=lease["fencing_token"],
                    reason_code="artifact_reserved",
                    evidence_json=reserve_evidence,
                    evidence_hash=_hash(reserve_evidence),
                    effective_at=now,
                    created_at=now,
                ),
            ]
        )
        await session.commit()

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolArtifact)
                .where(ToolArtifact.id == artifact_id)
                .values(
                    state="uploading",
                    resource_version=2,
                    updated_at=now + timedelta(seconds=1),
                )
            )
            await session.commit()
        await session.rollback()

    started_at = now + timedelta(seconds=2)
    start_evidence = {"upload": "started"}
    async with app.state.database.sessions() as session:
        session.add(
            ToolArtifactEvent(
                artifact_id=artifact_id,
                sequence=2,
                event="upload_start",
                previous_state="reserved",
                state="uploading",
                actor_type="provider",
                actor_id=lease["provider_id"],
                provider_id=lease["provider_id"],
                fencing_token=lease["fencing_token"],
                reason_code="artifact_upload_started",
                evidence_json=start_evidence,
                evidence_hash=_hash(start_evidence),
                effective_at=started_at,
                created_at=started_at,
            )
        )
        await session.flush()
        await session.execute(
            update(ToolArtifact)
            .where(ToolArtifact.id == artifact_id)
            .values(state="uploading", resource_version=2, updated_at=started_at)
        )
        await session.commit()

    async with app.state.database.sessions() as session:
        current = await session.get(ToolArtifact, artifact_id)
        assert current is not None
        assert current.state == "uploading"
        event = await session.scalar(
            select(ToolArtifactEvent).where(
                ToolArtifactEvent.artifact_id == artifact_id,
                ToolArtifactEvent.sequence == 2,
            )
        )
        assert event is not None
        event_id = event.id
        forged_at = now + timedelta(seconds=3)
        forged_evidence = {"upload": "complete", "forged": True}
        session.add(
            ToolArtifactEvent(
                artifact_id=artifact_id,
                sequence=3,
                event="upload_complete",
                previous_state="uploading",
                state="uploading",
                actor_type="provider",
                actor_id=lease["provider_id"],
                provider_id=lease["provider_id"],
                fencing_token=lease["fencing_token"],
                reason_code="artifact_upload_completed",
                evidence_json=forged_evidence,
                evidence_hash=_hash(forged_evidence),
                effective_at=forged_at,
                created_at=forged_at,
            )
        )
        await session.flush()
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolArtifact)
                .where(ToolArtifact.id == artifact_id)
                .values(
                    resource_version=3,
                    content_sha256="e" * 64,
                    byte_size=64,
                    width_pixels=1,
                    height_pixels=1,
                    referenced_at=forged_at,
                    updated_at=forged_at,
                )
            )
            await session.commit()
            await session.rollback()
        for statement in (
            update(ToolArtifactEvent)
            .where(ToolArtifactEvent.id == event_id)
            .values(reason_code="tampered"),
            delete(ToolArtifactEvent).where(ToolArtifactEvent.id == event_id),
            update(ToolArtifact)
            .where(ToolArtifact.id == artifact_id)
            .values(fencing_token=lease["fencing_token"] + 1),
            delete(ToolArtifact).where(ToolArtifact.id == artifact_id),
        ):
            with pytest.raises(DBAPIError):
                await session.execute(statement)
                await session.commit()
            await session.rollback()
