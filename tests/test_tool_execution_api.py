from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
import pytest

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
)
from superlily_core.models import (
    ToolAttempt,
    ToolAttemptEvent,
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    ToolInvocation,
    ToolProvider,
)
from superlily_core.settings import ToolRolloutScope
from superlily_core.tool_execution_service import reap_expired_attempts
from superlily_core.tool_registry_service import import_tool_descriptor, register_tool_provider
from superlily_provider_sdk import ProviderExecutionClient
from superlily_status_provider.executor import StatusProcessSupervisor
from superlily_status_provider.main import StatusProviderConfig, _execute_lease, _load_runtime


DESCRIPTOR_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.0.json"
)
EXECUTABLE_DESCRIPTOR_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.1.json"
)
PROVIDER_HEADERS = {"Authorization": "Bearer provider-status-secret"}


def invocation_payload(descriptor_hash: str, *, descriptor_version: str = "1.0.0") -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": "status.inspect",
        "descriptor_version": descriptor_version,
        "descriptor_hash": descriptor_hash,
        "input": {"scope": "provider_runtime"},
        "principal": {
            "platform": "qq",
            "sender_id": "123456",
            "conversation_id": "group:1080353942",
            "conversation_type": "group",
            "platform_roles": ["member"],
            "source_event_id": "qq:message:attempt-canary-1",
            "entry_id": "status-command-attempt-canary-1",
        },
        "capabilities": [],
    }


async def prepare_canary(
    client,
    app,
    *,
    implementation_hash: str = "a" * 64,
    descriptor_path: Path = DESCRIPTOR_PATH,
) -> tuple[ToolDescriptorRecord, str]:
    source = descriptor_path.read_bytes()
    authority = load_tool_descriptor(source).authority
    async with app.state.database.sessions() as session:
        descriptor, duplicate = await import_tool_descriptor(
            session,
            source,
            source_commit="2" * 40,
            bundle_hash=authority.sha256,
            reviewer="phase3b-attempt-reviewer",
        )
        assert duplicate is False
        stored = await session.get(ToolDescriptorRecord, descriptor.id)
        assert stored is not None
        stored.lifecycle = "active"
        session.add(
            ToolDescriptorLifecycleEvent(
                descriptor_id=stored.id,
                sequence=2,
                previous_lifecycle="reviewed",
                lifecycle="active",
                actor="test-reviewer",
                reason="test-only exact canary activation",
            )
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
        descriptor_version=descriptor.version,
        descriptor_hash=descriptor.descriptor_hash,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash=implementation_hash,
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
            **PROVIDER_HEADERS,
            "Idempotency-Key": "attempt-status-inventory-1",
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
        headers=PROVIDER_HEADERS,
    )
    assert heartbeat.status_code == 200, heartbeat.text
    scope = ToolRolloutScope(
        tool_id="status.inspect",
        descriptor_version=descriptor.version,
        descriptor_hash=descriptor.descriptor_hash,
        canonical_conversation="qq:group:1080353942",
        caller="admin_api",
        provider_id="provider-status-primary",
    )
    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode="canary",
        tool_canary_scopes=frozenset({scope}),
        tool_lease_seconds=10,
    )
    return descriptor, snapshot_hash


async def create_queued(client, descriptor: ToolDescriptorRecord, *, key: str) -> dict:
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
    assert body["state"] == "queued"
    assert body["policy"]["queue_created"] is True
    assert body["policy"]["lease_created"] is False
    assert body["policy"]["selected_provider_id"] == "provider-status-primary"
    return body


async def pull_lease(client, snapshot_hash: str):
    return await client.post(
        "/v1/tool-executions/lease",
        json={"schema_version": "1.0", "inventory_hash": snapshot_hash},
        headers=PROVIDER_HEADERS,
    )


def proof(lease: dict) -> dict:
    return {
        "schema_version": "1.0",
        "attempt_id": lease["attempt_id"],
        "fencing_token": lease["fencing_token"],
        "lease_secret": lease["lease_secret"],
    }


def successful_output(descriptor_hash: str) -> dict:
    return {
        "status": "ok",
        "checked_at": "2026-07-19T00:00:00Z",
        "scope": "provider_runtime",
        "provider_id": "provider-status-primary",
        "descriptor_hash": descriptor_hash,
        "implementation_hash": "a" * 64,
    }


def completion_usage(output: dict) -> dict:
    return ToolUsage(
        wall_time_ms=1,
        cpu_ms=1,
        memory_peak_bytes=1_048_576,
        input_bytes=len(
            canonicalize_json_value({"scope": "provider_runtime"}).canonical_bytes
        ),
        output_bytes=len(canonicalize_json_value(output).canonical_bytes),
    ).model_dump(mode="json")


async def test_exact_canary_lease_start_heartbeat_and_complete(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-canary-success-1")
    lease_response = await pull_lease(client, snapshot_hash)
    assert lease_response.status_code == 200, lease_response.text
    lease = lease_response.json()
    assert lease["invocation_id"] == invocation["invocation_id"]
    assert lease["attempt_number"] == 1
    assert lease["fencing_token"] == 1
    assert lease["provider_id"] == "provider-status-primary"
    assert len(lease["lease_secret"]) >= 32
    assert (await pull_lease(client, snapshot_hash)).status_code == 204

    started = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(lease),
        headers=PROVIDER_HEADERS,
    )
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "running"

    replay = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(lease),
        headers=PROVIDER_HEADERS,
    )
    assert replay.status_code == 409

    heartbeat = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/heartbeat",
        json={**proof(lease), "usage": ToolUsage(wall_time_ms=1).model_dump(mode="json")},
        headers=PROVIDER_HEADERS,
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["cancel_requested"] is False

    output = successful_output(descriptor.descriptor_hash)
    completed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "status-result-success-1",
            "output": output,
            "usage": completion_usage(output),
        },
        headers=PROVIDER_HEADERS,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "succeeded"
    assert completed.json()["output"] == output

    view = await client.get(
        f"/v1/tool-invocations/{invocation['invocation_id']}",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert view.status_code == 200
    evidence = view.json()
    assert evidence["state"] == "succeeded", (
        evidence["reason_code"],
        evidence["attempts"][0]["error_code"],
        evidence["attempts"][0]["usage"],
    )
    assert [item["event"] for item in view.json()["transitions"]] == [
        "propose",
        "queue",
        "lease",
        "start",
        "complete_success",
    ]
    assert view.json()["attempts"][0]["state"] == "succeeded"

    late = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "status-result-replay-1",
            "output": output,
            "usage": completion_usage(output),
        },
        headers=PROVIDER_HEADERS,
    )
    assert late.status_code == 409
    async with app.state.database.sessions() as session:
        events = (
            await session.scalars(
                select(ToolAttemptEvent)
                .where(ToolAttemptEvent.attempt_id == lease["attempt_id"])
                .order_by(ToolAttemptEvent.sequence)
            )
        ).all()
        assert [(item.event, item.outcome) for item in events] == [
            ("lease", "accepted"),
            ("start", "accepted"),
            ("reject", "rejected"),
            ("heartbeat", "accepted"),
            ("complete", "accepted"),
            ("reject", "rejected"),
        ]
        assert "lease_secret" not in json.dumps(
            [item.evidence_json for item in events], ensure_ascii=False
        )


async def test_each_stop_independently_prevents_a_new_lease(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-stops-1")

    scopes = app.state.settings.tool_canary_scopes
    unrelated_scope = replace(
        next(iter(scopes)),
        canonical_conversation="qq:group:708309706",
    )
    app.state.settings = replace(
        app.state.settings,
        tool_canary_scopes=frozenset({unrelated_scope}),
    )
    assert (await pull_lease(client, snapshot_hash)).status_code == 204
    app.state.settings = replace(app.state.settings, tool_canary_scopes=scopes)

    app.state.settings = replace(app.state.settings, tool_global_stop=True)
    assert (await pull_lease(client, snapshot_hash)).status_code == 204
    app.state.settings = replace(app.state.settings, tool_global_stop=False)

    async with app.state.database.sessions() as session:
        stored = await session.get(ToolDescriptorRecord, descriptor.id)
        assert stored is not None
        stored.lifecycle = "suspended"
        await session.commit()
    assert (await pull_lease(client, snapshot_hash)).status_code == 204
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolDescriptorRecord, descriptor.id)
        assert stored is not None
        stored.lifecycle = "active"
        provider = await session.get(ToolProvider, "provider-status-primary")
        assert provider is not None
        provider.lifecycle = "quarantined"
        await session.commit()
    assert (await pull_lease(client, snapshot_hash)).status_code == 204
    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, "provider-status-primary")
        assert provider is not None
        provider.lifecycle = "active"
        await session.commit()
    lease = await pull_lease(client, snapshot_hash)
    assert lease.status_code == 200, lease.text
    assert lease.json()["invocation_id"] == invocation["invocation_id"]


async def test_reviewed_enforce_scope_uses_its_own_exact_allowlist(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    exact_scope = next(iter(app.state.settings.tool_canary_scopes))
    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode="enforce",
        tool_canary_scopes=frozenset(),
        tool_enforce_scopes=frozenset({exact_scope}),
    )

    invocation = await create_queued(client, descriptor, key="attempt-enforce-exact-1")
    lease = await pull_lease(client, snapshot_hash)

    assert invocation["execution_mode"] == "enforce"
    assert lease.status_code == 200, lease.text
    assert lease.json()["invocation_id"] == invocation["invocation_id"]


async def test_sender_rate_limit_rejects_before_queueing_more_work(client, app) -> None:
    descriptor, _ = await prepare_canary(client, app)
    for index in range(10):
        await create_queued(client, descriptor, key=f"attempt-rate-{index}")
    limited = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(descriptor.descriptor_hash),
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "attempt-rate-limited",
        },
    )
    assert limited.status_code == 201
    assert limited.json()["state"] == "rejected"
    assert limited.json()["reason_code"] == "rate_limited"
    assert limited.json()["policy"]["queue_created"] is False


async def test_expired_lease_requeues_with_new_fence_and_rejects_old_worker(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-requeue-1")
    first = (await pull_lease(client, snapshot_hash)).json()
    async with app.state.database.sessions() as session:
        attempt = await session.get(ToolAttempt, first["attempt_id"])
        assert attempt is not None
        attempt.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()
    async with app.state.database.sessions() as session:
        assert await reap_expired_attempts(session) == [first["attempt_id"]]
    second_response = await pull_lease(client, snapshot_hash)
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["attempt_number"] == 2
    assert second["fencing_token"] == 2
    assert second["attempt_id"] != first["attempt_id"]

    stale = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(first),
        headers=PROVIDER_HEADERS,
    )
    assert stale.status_code == 409
    started = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(second),
        headers=PROVIDER_HEADERS,
    )
    assert started.status_code == 200
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolInvocation, invocation["invocation_id"])
        assert stored is not None and stored.state == "running"
        attempts = (
            await session.scalars(
                select(ToolAttempt)
                .where(ToolAttempt.invocation_id == invocation["invocation_id"])
                .order_by(ToolAttempt.attempt_number)
            )
        ).all()
        assert [item.state for item in attempts] == ["lease_expired", "running"]


async def test_cancellation_request_is_observed_and_acknowledged(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-cancel-1")
    lease = (await pull_lease(client, snapshot_hash)).json()
    assert (
        await client.post(
            f"/v1/tool-executions/{invocation['invocation_id']}/start",
            json=proof(lease),
            headers=PROVIDER_HEADERS,
        )
    ).status_code == 200
    cancelled = await client.post(
        f"/v1/tool-invocations/{invocation['invocation_id']}/cancel",
        json={"schema_version": "1.0", "reason": "operator cancellation test"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancel_requested"
    heartbeat = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/heartbeat",
        json={**proof(lease), "usage": ToolUsage(wall_time_ms=2).model_dump(mode="json")},
        headers=PROVIDER_HEADERS,
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["cancel_requested"] is True
    acknowledged = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/fail",
        json={
            **proof(lease),
            "provider_result_id": "status-result-cancel-1",
            "error_code": "cancelled",
            "safe_detail": "execution stopped after cancellation request",
            "usage": ToolUsage(wall_time_ms=3).model_dump(mode="json"),
        },
        headers=PROVIDER_HEADERS,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["state"] == "cancelled"


async def test_cancellation_before_start_can_be_acknowledged_without_unknown_state(
    client, app
) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-cancel-before-start-1")
    lease = (await pull_lease(client, snapshot_hash)).json()
    cancelled = await client.post(
        f"/v1/tool-invocations/{invocation['invocation_id']}/cancel",
        json={"schema_version": "1.0", "reason": "cancel before provider start"},
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancel_requested"

    acknowledged = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/fail",
        json={
            **proof(lease),
            "provider_result_id": "status-result-prestart-cancel-1",
            "error_code": "cancelled",
            "safe_detail": "lease cancelled before execution started",
            "usage": ToolUsage().model_dump(mode="json"),
        },
        headers=PROVIDER_HEADERS,
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["state"] == "cancelled"
    assert acknowledged.json()["started_at"] is None


async def test_core_lifespan_starts_and_stops_the_tool_reaper(app) -> None:
    reaper_task = None
    async with app.router.lifespan_context(app):
        reaper_task = next(
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "superlily-tool-reaper"
        )
        assert not reaper_task.done()
    assert reaper_task.done()
    assert reaper_task.cancelled()


async def test_wrong_secret_and_invalid_output_never_become_success(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-invalid-output-1")
    lease = (await pull_lease(client, snapshot_hash)).json()
    wrong = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json={**proof(lease), "lease_secret": "x" * 43},
        headers=PROVIDER_HEADERS,
    )
    assert wrong.status_code == 409
    assert (
        await client.post(
            f"/v1/tool-executions/{invocation['invocation_id']}/start",
            json=proof(lease),
            headers=PROVIDER_HEADERS,
        )
    ).status_code == 200
    invalid_output = {"status": "ok"}
    failed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "status-result-invalid-1",
            "output": invalid_output,
            "usage": ToolUsage(
                wall_time_ms=1,
                input_bytes=len(
                    canonicalize_json_value({"scope": "provider_runtime"}).canonical_bytes
                ),
                output_bytes=len(canonicalize_json_value(invalid_output).canonical_bytes),
            ).model_dump(mode="json"),
        },
        headers=PROVIDER_HEADERS,
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["error_code"] == "invalid_output"
    assert failed.json()["output"] is None
    view = await client.get(
        f"/v1/tool-invocations/{invocation['invocation_id']}",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert view.json()["state"] == "failed"
    assert view.json()["reason_code"] == "invalid_output"


async def test_live_budget_violation_requests_cancellation(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    invocation = await create_queued(client, descriptor, key="attempt-live-budget-1")
    lease = (await pull_lease(client, snapshot_hash)).json()
    assert (
        await client.post(
            f"/v1/tool-executions/{invocation['invocation_id']}/start",
            json=proof(lease),
            headers=PROVIDER_HEADERS,
        )
    ).status_code == 200
    over_budget = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/heartbeat",
        json={
            **proof(lease),
            "usage": ToolUsage(wall_time_ms=5_001).model_dump(mode="json"),
            "provider_observed_at": "2099-01-01T00:00:00+00:00",
        },
        headers=PROVIDER_HEADERS,
    )
    assert over_budget.status_code == 200, over_budget.text
    assert over_budget.json()["cancel_requested"] is True
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolInvocation, invocation["invocation_id"])
        assert stored is not None
        assert stored.state == "cancel_requested"
        assert stored.reason_code == "budget_exceeded"


async def test_concurrent_pull_issues_only_one_active_lease(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    await create_queued(client, descriptor, key="attempt-concurrent-lease-1")

    first, second = await asyncio.gather(
        pull_lease(client, snapshot_hash),
        pull_lease(client, snapshot_hash),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 204]
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolAttempt.id))) == 1


async def test_attempt_events_are_database_append_only(client, app) -> None:
    descriptor, snapshot_hash = await prepare_canary(client, app)
    await create_queued(client, descriptor, key="attempt-event-trigger-1")
    lease = (await pull_lease(client, snapshot_hash)).json()
    async with app.state.database.sessions() as session:
        event = await session.scalar(
            select(ToolAttemptEvent).where(ToolAttemptEvent.attempt_id == lease["attempt_id"])
        )
        assert event is not None
        event.reason_code = "tampered"
        with pytest.raises(DBAPIError, match="append-only"):
            await session.commit()


async def test_real_status_supervisor_completes_through_core_lease_protocol(
    client, app
) -> None:
    _, implementation = _load_runtime(
        EXECUTABLE_DESCRIPTOR_PATH,
        execution_enabled=True,
    )
    implementation_hash = implementation.inventory_entry.implementation_hash
    descriptor, inventory_hash = await prepare_canary(
        client,
        app,
        implementation_hash=implementation_hash,
        descriptor_path=EXECUTABLE_DESCRIPTOR_PATH,
    )
    invocation = await create_queued(client, descriptor, key="attempt-real-status-e2e-1")
    execution_client = ProviderExecutionClient(
        base_url="http://test",
        provider_id="provider-status-primary",
        token="provider-status-secret",
        client=client,
    )
    lease = await execution_client.request_lease(inventory_hash)
    assert lease is not None
    supervisor = StatusProcessSupervisor(
        EXECUTABLE_DESCRIPTOR_PATH.read_bytes(),
        implementation_hash=implementation_hash,
    )
    config = StatusProviderConfig(
        core_url="http://test",
        token="provider-status-secret",
        descriptor_path=EXECUTABLE_DESCRIPTOR_PATH,
        execution_heartbeat_seconds=0.1,
    )

    await _execute_lease(
        execution_client,
        supervisor,
        implementation,
        lease,
        config,
        inventory_hash=inventory_hash,
    )

    view = await client.get(
        f"/v1/tool-invocations/{invocation['invocation_id']}",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert view.status_code == 200
    evidence = view.json()
    assert evidence["state"] == "succeeded", (
        evidence["reason_code"],
        evidence["attempts"][0]["error_code"],
        evidence["attempts"][0]["usage"],
    )
    assert evidence["attempts"][0]["state"] == "succeeded"
    assert evidence["attempts"][0]["output"]["implementation_hash"] == implementation_hash
