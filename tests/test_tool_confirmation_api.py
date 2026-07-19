from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select, update

from superlily_core.models import (
    ToolConfirmation,
    ToolInvocation,
    ToolRolloutPlanCounter,
    ToolRolloutPlanLifecycleEvent,
    ToolRolloutPlanRecord,
)
from superlily_core import tool_invocation_service
from superlily_core.tool_invocation_service import reap_expired_invocations

from test_tool_execution_api import (
    PROVIDER_HEADERS,
    invocation_payload,
    prepare_canary,
    pull_lease,
)


BASE_DESCRIPTOR = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.2.json"
)


def _confirmation_descriptor(tmp_path: Path, *, policy: str = "always") -> Path:
    document = json.loads(BASE_DESCRIPTOR.read_bytes())
    document["confirmation"] = policy
    path = tmp_path / f"status-inspect-confirmation-{policy}.json"
    path.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )
    return path


async def _prepare_confirmation_canary(client, app, tmp_path: Path):
    return await prepare_canary(
        client,
        app,
        descriptor_path=_confirmation_descriptor(tmp_path),
    )


async def _create_pending(client, descriptor, *, key: str) -> dict:
    response = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(
            descriptor.descriptor_hash,
            descriptor_version=descriptor.version,
        ),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": key,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["state"] == "awaiting_confirmation"
    assert body["reason_code"] == "confirmation_required"
    assert body["policy"]["queue_created"] is False
    assert body["policy"]["confirmation_challenge"]["confirmation_id"] == (
        body["confirmation"]["confirmation_id"]
    )
    assert [item["event"] for item in body["transitions"]] == [
        "propose",
        "require_confirmation",
    ]
    return body


def _decision(body: dict, decision: str = "approve") -> dict:
    return {
        "schema_version": "1.0",
        "confirmation_id": body["confirmation"]["confirmation_id"],
        "request_hash": body["request"]["request_hash"],
        "input_hash": body["request"]["input_hash"],
        "principal_hash": body["request"]["principal_hash"],
        "decision": decision,
        "reason": "用户明确批准这次精确调用" if decision == "approve" else "用户拒绝这次调用",
    }


async def _counter(app) -> int:
    async with app.state.database.sessions() as session:
        value = await session.scalar(
            select(ToolRolloutPlanCounter.consumed_invocations)
        )
    assert value is not None
    return value


async def test_confirmation_approval_consumes_once_then_queues_and_leases(
    client, app, tmp_path
) -> None:
    descriptor, inventory_hash = await _prepare_confirmation_canary(
        client, app, tmp_path
    )
    pending = await _create_pending(
        client, descriptor, key="confirmation-approve-create-1"
    )
    assert await _counter(app) == 0
    assert (await pull_lease(client, inventory_hash)).status_code == 204

    decision = _decision(pending)
    approved = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=decision,
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-approve-decision-1",
        },
    )
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["duplicate"] is False
    assert body["state"] == "queued"
    assert body["reason_code"] == "confirmation_approved"
    assert body["confirmation"]["state"] == "consumed"
    assert [item["event"] for item in body["transitions"]] == [
        "propose",
        "require_confirmation",
        "confirm",
    ]
    assert await _counter(app) == 1

    replay = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=decision,
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-approve-decision-1",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["state"] == "queued"
    assert await _counter(app) == 1

    changed = {**decision, "reason": "同一个键却换了理由"}
    conflict = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=changed,
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-approve-decision-1",
        },
    )
    assert conflict.status_code == 409

    lease = await pull_lease(client, inventory_hash)
    assert lease.status_code == 200, lease.text
    assert lease.json()["invocation_id"] == pending["invocation_id"]


async def test_confirmation_requires_original_caller_and_exact_hashes(
    client, app, tmp_path
) -> None:
    descriptor, _ = await _prepare_confirmation_canary(client, app, tmp_path)
    pending = await _create_pending(
        client, descriptor, key="confirmation-binding-create-1"
    )
    decision = _decision(pending)
    wrong_caller = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=decision,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "confirmation-binding-caller-1",
        },
    )
    assert wrong_caller.status_code == 404

    wrong_hash = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json={**decision, "input_hash": "f" * 64},
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-binding-hash-1",
        },
    )
    assert wrong_hash.status_code == 409
    assert await _counter(app) == 0
    async with app.state.database.sessions() as session:
        invocation = await session.get(ToolInvocation, pending["invocation_id"])
        confirmation = await session.get(
            ToolConfirmation, pending["confirmation"]["confirmation_id"]
        )
    assert invocation is not None and invocation.state == "awaiting_confirmation"
    assert confirmation is not None and confirmation.state == "pending"


async def test_confirmation_reject_and_cancel_never_create_a_lease(
    client, app, tmp_path
) -> None:
    descriptor, inventory_hash = await _prepare_confirmation_canary(
        client, app, tmp_path
    )
    rejected_pending = await _create_pending(
        client, descriptor, key="confirmation-reject-create-1"
    )
    rejected = await client.post(
        f"/v1/tool-invocations/{rejected_pending['invocation_id']}/confirm",
        json=_decision(rejected_pending, "reject"),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-reject-decision-1",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["state"] == "rejected"
    assert rejected.json()["confirmation"]["state"] == "rejected"

    cancelled_pending = await _create_pending(
        client, descriptor, key="confirmation-cancel-create-1"
    )
    cancelled = await client.post(
        f"/v1/tool-invocations/{cancelled_pending['invocation_id']}/cancel",
        json={"schema_version": "1.0", "reason": "用户撤销等待中的调用"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["confirmation"]["state"] == "rejected"
    assert await _counter(app) == 0
    assert (await pull_lease(client, inventory_hash)).status_code == 204


async def test_confirmation_approval_fails_closed_after_rollout_pause(
    client, app, tmp_path
) -> None:
    descriptor, inventory_hash = await _prepare_confirmation_canary(
        client, app, tmp_path
    )
    pending = await _create_pending(
        client, descriptor, key="confirmation-pause-create-1"
    )
    now = datetime.now(timezone.utc)
    async with app.state.database.sessions() as session:
        plan = await session.scalar(
            select(ToolRolloutPlanRecord).where(
                ToolRolloutPlanRecord.lifecycle == "active"
            )
        )
        assert plan is not None
        next_version = plan.resource_version + 1
        session.add(
            ToolRolloutPlanLifecycleEvent(
                plan_record_id=plan.id,
                sequence=next_version,
                previous_lifecycle="active",
                lifecycle="paused",
                actor="test-operator",
                reason="测试确认等待期间暂停 rollout",
                created_at=now,
            )
        )
        await session.flush()
        await session.execute(
            update(ToolRolloutPlanRecord)
            .where(ToolRolloutPlanRecord.id == plan.id)
            .values(
                lifecycle="paused",
                resource_version=next_version,
                updated_at=now,
            )
        )
        await session.commit()

    response = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=_decision(pending),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-pause-decision-1",
        },
    )
    assert response.status_code == 409
    replay = await client.post(
        f"/v1/tool-invocations/{pending['invocation_id']}/confirm",
        json=_decision(pending),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-pause-decision-1",
        },
    )
    assert replay.status_code == 409
    async with app.state.database.sessions() as session:
        invocation = await session.get(ToolInvocation, pending["invocation_id"])
        confirmation = await session.get(
            ToolConfirmation, pending["confirmation"]["confirmation_id"]
        )
    assert invocation is not None and invocation.state == "rejected"
    assert invocation.reason_code == "rollout_plan_inactive"
    assert confirmation is not None and confirmation.state == "rejected"
    assert await _counter(app) == 0
    assert (await pull_lease(client, inventory_hash)).status_code == 204


async def test_concurrent_confirmation_approval_has_one_counter_winner(
    client, app, tmp_path
) -> None:
    descriptor, _ = await _prepare_confirmation_canary(client, app, tmp_path)
    pending = await _create_pending(
        client, descriptor, key="confirmation-concurrent-create-1"
    )
    url = f"/v1/tool-invocations/{pending['invocation_id']}/confirm"
    headers = {
        "Authorization": "Bearer admin-secret",
        "Idempotency-Key": f"confirmation-concurrent-{uuid4()}",
    }
    first, second = await asyncio.gather(
        client.post(url, json=_decision(pending), headers=headers),
        client.post(url, json=_decision(pending), headers=headers),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert sorted([first.json()["duplicate"], second.json()["duplicate"]]) == [False, True]
    assert await _counter(app) == 1
    async with app.state.database.sessions() as session:
        count = await session.scalar(
            select(func.count(ToolConfirmation.id)).where(
                ToolConfirmation.state == "consumed"
            )
        )
    assert count == 1


async def test_confirmation_reaper_expires_challenge_and_invocation_together(
    client, app, tmp_path, monkeypatch
) -> None:
    descriptor, inventory_hash = await _prepare_confirmation_canary(
        client, app, tmp_path
    )
    pending = await _create_pending(
        client, descriptor, key="confirmation-expiry-create-1"
    )
    expires_at = datetime.fromisoformat(pending["confirmation"]["expires_at"])

    async def after_expiry(_session):
        return expires_at + timedelta(seconds=1)

    monkeypatch.setattr(tool_invocation_service, "database_now", after_expiry)
    async with app.state.database.sessions() as session:
        assert await reap_expired_invocations(session) == [pending["invocation_id"]]

    view = await client.get(
        f"/v1/tool-invocations/{pending['invocation_id']}",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert view.status_code == 200
    assert view.json()["state"] == "expired"
    assert view.json()["reason_code"] == "deadline_expired"
    assert view.json()["confirmation"]["state"] == "expired"
    assert await _counter(app) == 0
    assert (await pull_lease(client, inventory_hash)).status_code == 204


async def test_two_person_confirmation_remains_fail_closed(client, app, tmp_path) -> None:
    descriptor, inventory_hash = await prepare_canary(
        client,
        app,
        descriptor_path=_confirmation_descriptor(tmp_path, policy="two_person"),
    )
    response = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(
            descriptor.descriptor_hash,
            descriptor_version=descriptor.version,
        ),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "confirmation-two-person-create-1",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "rejected"
    assert response.json()["reason_code"] == "confirmation_unavailable"
    assert response.json()["confirmation"] is None
    assert await _counter(app) == 0
    assert (await pull_lease(client, inventory_hash)).status_code == 204
