from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    canonicalize_json_value,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
)
from superlily_core.auth import InvocationIdentity
from superlily_core.models import (
    ToolDescriptorRecord,
    ToolDescriptorLifecycleEvent,
    ToolInvocation,
    ToolInvocationTransition,
)
from superlily_core.tool_invocation_service import (
    append_invocation_transition,
    reap_expired_invocations,
)
from superlily_core.tool_registry_service import import_tool_descriptor, register_tool_provider


DESCRIPTOR_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.0.json"
)


async def import_descriptor(app, *, allowed_callers: list[str] | None = None):
    document = json.loads(DESCRIPTOR_PATH.read_bytes())
    if allowed_callers is not None:
        document["allowed_callers"] = allowed_callers
    source = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode()
    authority = load_tool_descriptor(source).authority
    async with app.state.database.sessions() as session:
        record, duplicate = await import_tool_descriptor(
            session,
            source,
            source_commit="1" * 40,
            bundle_hash=authority.sha256,
            reviewer="phase3b-reviewer",
        )
    assert duplicate is False
    return record


def invocation_payload(descriptor_hash: str) -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": "status.inspect",
        "descriptor_version": "1.0.0",
        "descriptor_hash": descriptor_hash,
        "input": {"scope": "provider_runtime"},
        "principal": {
            "platform": "qq",
            "sender_id": "123456",
            "conversation_id": "group:1080353942",
            "conversation_type": "group",
            "platform_roles": ["member"],
            "source_event_id": "qq:message:ledger-only-1",
            "entry_id": "status-command-ledger-only-1",
        },
        "capabilities": [],
    }


def admin_headers(idempotency_key: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer admin-secret",
        "Idempotency-Key": idempotency_key,
    }


async def make_descriptor_and_provider_eligible(client, app, descriptor) -> str:
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolDescriptorRecord, descriptor.id)
        assert stored is not None
        session.add(
            ToolDescriptorLifecycleEvent(
                descriptor_id=stored.id,
                sequence=stored.resource_version + 1,
                previous_lifecycle=stored.lifecycle,
                lifecycle="active",
                actor="test-reviewer",
                reason="test-only activation",
            )
        )
        await session.flush()
        await session.execute(
            update(ToolDescriptorRecord)
            .where(ToolDescriptorRecord.id == stored.id)
            .values(lifecycle="active", resource_version=stored.resource_version + 1)
        )
        await session.commit()
    registration = ProviderRegistration(
        provider_id="provider-status-primary",
        owner="superlily-operations",
        lifecycle="active",
        allowed_protocols=["superlily-provider-pull-v1"],
        tool_selectors=["status.inspect"],
    )
    async with app.state.database.sessions() as session:
        _, duplicate = await register_tool_provider(
            session,
            registration,
            actor="test-reviewer",
            settings=app.state.settings,
        )
        assert duplicate is False
    tool = ProviderInventoryTool(
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash=descriptor.descriptor_hash,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash="a" * 64,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )
    snapshot_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[tool],
    )
    now = datetime.now(timezone.utc).isoformat()
    inventory = await client.post(
        "/v1/provider-inventory/snapshots",
        json={
            "schema_version": "1.0",
            "provider_id": "provider-status-primary",
            "snapshot_hash": snapshot_hash,
            "observed_at": now,
            "protocol_version": "superlily-provider-pull-v1",
            "tools": [tool.model_dump(mode="json")],
        },
        headers={
            "Authorization": "Bearer provider-status-secret",
            "Idempotency-Key": "eligible-status-inventory-1",
        },
    )
    assert inventory.status_code == 201, inventory.text
    heartbeat = await client.post(
        "/v1/providers/heartbeats",
        json={
            "schema_version": "1.0",
            "provider_id": "provider-status-primary",
            "inventory_hash": snapshot_hash,
            "observed_at": now,
            "health": "healthy",
            "current_concurrency": 0,
            "max_concurrency": 4,
            "metadata": {},
        },
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return snapshot_hash


async def test_ledger_only_records_policy_evidence_without_queue_or_lease(client, app) -> None:
    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    descriptor = await import_descriptor(app)
    payload = invocation_payload(descriptor.descriptor_hash)

    created = await client.post(
        "/v1/tool-invocations",
        json=payload,
        headers=admin_headers("ledger-only-status-1"),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["duplicate"] is False
    assert body["state"] == "recorded_only"
    assert body["reason_code"] == "ledger_only"
    assert body["execution_mode"] == "ledger_only"
    assert body["policy"]["decision"] == "recorded_only"
    assert body["policy"]["eligible_if_execution_enabled"] is False
    assert body["policy"]["queue_created"] is False
    assert body["policy"]["lease_created"] is False
    assert body["policy"]["effective_reasons"] == [
        "inactive_descriptor",
        "provider_missing",
    ]
    assert [item["event"] for item in body["transitions"]] == [
        "propose",
        "record_only",
    ]
    assert [item["sequence"] for item in body["transitions"]] == [1, 2]
    lease = await client.post(
        "/v1/tool-executions/lease",
        json={"schema_version": "1.0", "inventory_hash": "a" * 64},
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert lease.status_code == 204

    replay = await client.post(
        "/v1/tool-invocations",
        json=payload,
        headers=admin_headers("ledger-only-status-1"),
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["invocation_id"] == body["invocation_id"]

    changed = invocation_payload(descriptor.descriptor_hash)
    changed["principal"]["sender_id"] = "654321"
    conflict = await client.post(
        "/v1/tool-invocations",
        json=changed,
        headers=admin_headers("ledger-only-status-1"),
    )
    assert conflict.status_code == 409

    assert (
        await client.get(
            f"/v1/tool-invocations/{body['invocation_id']}",
            headers={"Authorization": "Bearer lily-secret"},
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/v1/tool-invocations/{body['invocation_id']}",
            headers={"Authorization": "Bearer provider-status-secret"},
        )
    ).status_code == 401
    assert (
        await client.post(
            f"/v1/tool-invocations/{body['invocation_id']}/cancel",
            json={"schema_version": "1.0", "reason": "already terminal"},
            headers={"Authorization": "Bearer admin-secret"},
        )
    ).status_code == 409

    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolInvocation.id))) == 1
        assert await session.scalar(select(func.count(ToolInvocationTransition.id))) == 2


async def test_execution_off_and_invalid_authority_create_no_ledger_rows(client, app) -> None:
    descriptor = await import_descriptor(app)
    payload = invocation_payload(descriptor.descriptor_hash)
    off = await client.post(
        "/v1/tool-invocations",
        json=payload,
        headers=admin_headers("execution-off-status-1"),
    )
    assert off.status_code == 409

    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    invalid_input = invocation_payload(descriptor.descriptor_hash)
    invalid_input["input"] = {"scope": "host"}
    assert (
        await client.post(
            "/v1/tool-invocations",
            json=invalid_input,
            headers=admin_headers("invalid-input-status-1"),
        )
    ).status_code == 422
    wrong_hash = invocation_payload("f" * 64)
    assert (
        await client.post(
            "/v1/tool-invocations",
            json=wrong_hash,
            headers=admin_headers("wrong-hash-status-1"),
        )
    ).status_code == 409

    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolInvocation.id))) == 0
        assert await session.scalar(select(func.count(ToolInvocationTransition.id))) == 0


async def test_identity_bound_caller_policy_is_rejected_and_audited(client, app) -> None:
    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    descriptor = await import_descriptor(app, allowed_callers=["command"])
    response = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(descriptor.descriptor_hash),
        headers=admin_headers("caller-forbidden-status-1"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "rejected"
    assert body["reason_code"] == "caller_forbidden"
    assert "caller_forbidden" in body["policy"]["effective_reasons"]
    assert [item["event"] for item in body["transitions"]] == ["propose", "reject"]


async def test_eligible_ledger_only_proposal_still_creates_no_queue_or_lease(client, app) -> None:
    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    descriptor = await import_descriptor(app)
    await make_descriptor_and_provider_eligible(client, app, descriptor)

    response = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(descriptor.descriptor_hash),
        headers=admin_headers("eligible-ledger-status-1"),
    )
    assert response.status_code == 201, response.text
    policy = response.json()["policy"]
    assert response.json()["state"] == "recorded_only"
    assert policy["eligible_if_execution_enabled"] is True
    assert policy["effective_reasons"] == []
    assert policy["queue_created"] is False
    assert policy["lease_created"] is False
    registry = await client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert registry.json()["execution"] == {
        "mode": "ledger_only",
        "global_stop": False,
        "invocation_endpoints": True,
        "lease_endpoint": True,
        "leases_enabled": False,
        "natural_language_callers": False,
    }
    assert registry.json()["summary"]["eligible_tools"] == 1

    app.state.settings = replace(app.state.settings, tool_global_stop=True)
    stopped = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(descriptor.descriptor_hash),
        headers=admin_headers("global-stop-ledger-status-1"),
    )
    assert stopped.status_code == 201
    assert stopped.json()["state"] == "recorded_only"
    assert stopped.json()["policy"]["eligible_if_execution_enabled"] is False
    assert stopped.json()["policy"]["effective_reasons"] == ["global_stop"]
    assert stopped.json()["policy"]["queue_created"] is False
    assert stopped.json()["policy"]["lease_created"] is False


async def test_command_invocation_is_scoped_to_its_authenticated_instance(client, app) -> None:
    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    descriptor = await import_descriptor(app)
    created = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(descriptor.descriptor_hash),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "command-status-ledger-1",
        },
    )
    assert created.status_code == 201
    invocation_id = created.json()["invocation_id"]
    assert created.json()["creator"] == {"type": "command", "id": "lily-command"}
    assert (
        await client.get(
            f"/v1/tool-invocations/{invocation_id}",
            headers={"Authorization": "Bearer nekro-secret"},
        )
    ).status_code == 404
    assert (
        await client.get(
            f"/v1/tool-invocations/{invocation_id}",
            headers={"Authorization": "Bearer admin-secret"},
        )
    ).status_code == 200


async def test_concurrent_invocation_replay_creates_one_immutable_ledger(client, app) -> None:
    app.state.settings = replace(app.state.settings, tool_execution_mode="ledger_only")
    descriptor = await import_descriptor(app)
    payload = invocation_payload(descriptor.descriptor_hash)
    headers = admin_headers("concurrent-ledger-status-1")

    first, second = await asyncio.gather(
        client.post("/v1/tool-invocations", json=payload, headers=headers),
        client.post("/v1/tool-invocations", json=payload, headers=headers),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["invocation_id"] == second.json()["invocation_id"]
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolInvocation.id))) == 1
        assert await session.scalar(select(func.count(ToolInvocationTransition.id))) == 2


async def _seed_proposed_invocation(app, descriptor: ToolDescriptorRecord) -> str:
    now = datetime.now(timezone.utc)
    invocation_id = str(uuid4())
    snapshot = canonicalize_json_value({"seed": True})
    invocation = ToolInvocation(
        id=invocation_id,
        creator_type="admin_api",
        creator_id="core-admin",
        idempotency_key=f"seed-{invocation_id}",
        request_hash=canonicalize_json_value({"request": invocation_id}).sha256,
        descriptor_id=descriptor.id,
        tool_id=descriptor.tool_id,
        descriptor_version=descriptor.version,
        descriptor_hash=descriptor.descriptor_hash,
        descriptor_snapshot_json=descriptor.descriptor_json,
        input_json={"scope": "provider_runtime"},
        input_hash=canonicalize_json_value({"scope": "provider_runtime"}).sha256,
        principal_snapshot_json={"authenticated_subject": "core-admin"},
        principal_hash=snapshot.sha256,
        capability_snapshot_json=[],
        capability_hash=canonicalize_json_value([]).sha256,
        policy_snapshot_json={"seed": True},
        policy_hash=snapshot.sha256,
        execution_mode="ledger_only",
        state="proposed",
        transition_sequence=1,
        reason_code="proposal_received",
        deadline_at=now + timedelta(minutes=1),
        terminal_at=None,
        created_at=now,
        updated_at=now,
    )
    transition = ToolInvocationTransition(
        invocation_id=invocation_id,
        sequence=1,
        event="propose",
        previous_state=None,
        state="proposed",
        actor_type="admin_api",
        actor_id="core-admin",
        reason_code="proposal_received",
        evidence_json={"seed": True},
        evidence_hash=snapshot.sha256,
        created_at=now,
    )
    async with app.state.database.sessions() as session:
        session.add_all([invocation, transition])
        await session.commit()
    return invocation_id


async def test_transition_cas_cancellation_and_append_only_trigger(client, app) -> None:
    descriptor = await import_descriptor(app)
    invocation_id = await _seed_proposed_invocation(app, descriptor)
    async with app.state.database.sessions() as session:
        queued = await append_invocation_transition(
            session,
            invocation_id,
            event="queue",
            state="queued",
            actor_type="system",
            actor_id="test-policy",
            reason_code="eligible",
            evidence={"eligible": True},
        )
        assert queued.state == "queued"
        with pytest.raises(HTTPException, match="illegal invocation transition"):
            await append_invocation_transition(
                session,
                invocation_id,
                event="complete_success",
                state="succeeded",
                actor_type="system",
                actor_id="invalid-test",
                reason_code="invalid",
                evidence={},
            )

    cancelled = await client.post(
        f"/v1/tool-invocations/{invocation_id}/cancel",
        json={"schema_version": "1.0", "reason": "operator test"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert [item["sequence"] for item in cancelled.json()["transitions"]] == [1, 2, 3]

    async with app.state.database.sessions() as session:
        transition = await session.scalar(
            select(ToolInvocationTransition).where(
                ToolInvocationTransition.invocation_id == invocation_id,
                ToolInvocationTransition.sequence == 2,
            )
        )
        assert transition is not None
        transition_id = transition.id
        transition.reason_code = "tampered"
        with pytest.raises(DBAPIError, match="append-only"):
            await session.commit()
        await session.rollback()
        stored = await session.get(ToolInvocationTransition, transition_id)
        assert stored is not None and stored.reason_code == "eligible"


async def test_concurrent_cancellation_has_one_cas_winner(client, app) -> None:
    descriptor = await import_descriptor(app)
    invocation_id = await _seed_proposed_invocation(app, descriptor)
    async with app.state.database.sessions() as session:
        await append_invocation_transition(
            session,
            invocation_id,
            event="queue",
            state="queued",
            actor_type="system",
            actor_id="test-policy",
            reason_code="eligible",
            evidence={},
        )
    request = {"schema_version": "1.0", "reason": "concurrent operator test"}
    headers = {"Authorization": "Bearer admin-secret"}

    first, second = await asyncio.gather(
        client.post(
            f"/v1/tool-invocations/{invocation_id}/cancel",
            json=request,
            headers=headers,
        ),
        client.post(
            f"/v1/tool-invocations/{invocation_id}/cancel",
            json=request,
            headers=headers,
        ),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    async with app.state.database.sessions() as session:
        invocation = await session.get(ToolInvocation, invocation_id)
        transitions = (
            await session.scalars(
                select(ToolInvocationTransition)
                .where(ToolInvocationTransition.invocation_id == invocation_id)
                .order_by(ToolInvocationTransition.sequence)
            )
        ).all()
        assert invocation is not None and invocation.state == "cancelled"
        assert [item.state for item in transitions] == ["proposed", "queued", "cancelled"]


async def test_reaper_uses_database_deadline_and_conservative_outcomes(app) -> None:
    descriptor = await import_descriptor(app)
    queued_id = await _seed_proposed_invocation(app, descriptor)
    running_id = await _seed_proposed_invocation(app, descriptor)
    async with app.state.database.sessions() as session:
        await append_invocation_transition(
            session,
            queued_id,
            event="queue",
            state="queued",
            actor_type="system",
            actor_id="test-policy",
            reason_code="eligible",
            evidence={},
        )
    async with app.state.database.sessions() as session:
        await append_invocation_transition(
            session,
            running_id,
            event="queue",
            state="queued",
            actor_type="system",
            actor_id="test-policy",
            reason_code="eligible",
            evidence={},
        )
        await append_invocation_transition(
            session,
            running_id,
            event="lease",
            state="leased",
            actor_type="provider",
            actor_id="provider-status-primary",
            reason_code="leased",
            evidence={},
        )
        await append_invocation_transition(
            session,
            running_id,
            event="start",
            state="running",
            actor_type="provider",
            actor_id="provider-status-primary",
            reason_code="started",
            evidence={},
        )
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with app.state.database.sessions() as session:
        await session.execute(
            update(ToolInvocation)
            .where(ToolInvocation.id.in_([queued_id, running_id]))
            .values(deadline_at=past)
        )
        await session.commit()
    async with app.state.database.sessions() as session:
        transitioned = await reap_expired_invocations(session, limit=10)
        queued = await session.get(ToolInvocation, queued_id)
        running = await session.get(ToolInvocation, running_id)
        assert set(transitioned) == {queued_id, running_id}
        assert queued is not None and queued.state == "timed_out"
        assert running is not None and running.state == "unknown_completion"
