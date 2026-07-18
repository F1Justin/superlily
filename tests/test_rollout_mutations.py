from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    load_tool_descriptor,
    load_tool_rollout_plan,
    provider_inventory_snapshot_hash,
)
from superlily_core.control_plane import CONTROL_CSRF_HEADER, hash_control_password
from superlily_core import tool_registry_admin
from superlily_core.database import Database
from superlily_core.models import (
    ControlPlaneAuditEvent,
    ControlPlaneMutation,
    ToolAttempt,
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    ToolInvocation,
    ToolProvider,
    ToolRolloutPlanCounter,
    ToolRolloutPlanItemRecord,
    ToolRolloutPlanLifecycleEvent,
    ToolRolloutPlanRecord,
)
from superlily_core.rollout_service import import_tool_rollout_plan
from superlily_core.settings import ControlOperator
from superlily_core.tool_registry_service import (
    import_tool_descriptor,
    register_tool_provider,
)


DESCRIPTOR_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.0.json"
)
DESCRIPTOR_SOURCE = DESCRIPTOR_PATH.read_bytes()
DESCRIPTOR_HASH = load_tool_descriptor(DESCRIPTOR_SOURCE).authority.sha256
PROVIDER_ID = "provider-status-primary"
CONTROL_PASSWORD = "correct horse battery staple"
CONTROL_PASSWORD_HASH = hash_control_password(CONTROL_PASSWORD, salt=b"rollout-salt-001")
CONTROL_ORIGIN = "https://control.test"


def _enable_control(app, *, role: str = "operator", mode: str = "canary") -> None:
    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode=mode,
        control_operators={
            "rollout.user": ControlOperator(
                operator_id="rollout.user",
                role=role,
                password_hash=CONTROL_PASSWORD_HASH,
            )
        },
        control_allowed_hosts=frozenset({"control.test"}),
        control_allowed_origins=frozenset({CONTROL_ORIGIN}),
        control_audit_pepper="rollout-mutation-test-pepper-32-bytes",
    )


def _control_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=CONTROL_ORIGIN,
    )


async def _login(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/v1/control/session/login",
        json={
            "schema_version": "1.0",
            "operator_id": "rollout.user",
            "password": CONTROL_PASSWORD,
        },
        headers={"origin": CONTROL_ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _plan_source(
    *,
    max_invocations: int = 1,
    expected_descriptor_version: int = 2,
    expected_provider_version: int = 1,
    version: str = "1.0.0",
) -> bytes:
    now = datetime.now(timezone.utc)
    return json.dumps(
        {
            "schema_version": "1.0",
            "plan_id": "status-inspect-controlled-canary",
            "version": version,
            "mode": "canary",
            "starts_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "max_invocations": max_invocations,
            "rollback_mode": "ledger_only",
            "reason": "One reviewed status inspection canary",
            "items": [
                {
                    "item_id": "status-inspect-admin",
                    "tool_id": "status.inspect",
                    "descriptor_version": "1.0.0",
                    "descriptor_hash": DESCRIPTOR_HASH,
                    "canonical_conversation": "qq:group:1080353942",
                    "caller": "admin_api",
                    "provider_id": PROVIDER_ID,
                    "expected_descriptor_resource_version": expected_descriptor_version,
                    "expected_provider_resource_version": expected_provider_version,
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


async def _import_plan(app, source: bytes) -> ToolRolloutPlanRecord:
    digest = load_tool_rollout_plan(source).authority.sha256
    async with app.state.database.sessions() as session:
        plan, duplicate = await import_tool_rollout_plan(
            session,
            source,
            source_commit="4" * 40,
            bundle_hash=digest,
            reviewer="rollout-git-reviewer",
        )
    assert duplicate is False
    return plan


async def _setup_authority(
    app,
    client,
    *,
    max_invocations: int = 1,
    expected_descriptor_version: int = 2,
    expected_provider_version: int = 1,
):
    async with app.state.database.sessions() as session:
        descriptor, duplicate = await import_tool_descriptor(
            session,
            DESCRIPTOR_SOURCE,
            source_commit="1" * 40,
            bundle_hash=DESCRIPTOR_HASH,
            reviewer="descriptor-git-reviewer",
        )
        assert duplicate is False
        session.add(
            ToolDescriptorLifecycleEvent(
                descriptor_id=descriptor.id,
                sequence=2,
                previous_lifecycle="reviewed",
                lifecycle="active",
                actor="descriptor-reviewer",
                reason="test-only reviewed descriptor activation",
            )
        )
        await session.flush()
        await session.execute(
            update(ToolDescriptorRecord)
            .where(ToolDescriptorRecord.id == descriptor.id)
            .values(lifecycle="active", resource_version=2)
        )
        await session.commit()
    async with app.state.database.sessions() as session:
        provider, duplicate = await register_tool_provider(
            session,
            ProviderRegistration(
                provider_id=PROVIDER_ID,
                owner="superlily-operations",
                lifecycle="active",
                allowed_protocols=["superlily-provider-pull-v1"],
                tool_selectors=["status.inspect"],
            ),
            actor="provider-git-reviewer",
            settings=app.state.settings,
        )
    assert duplicate is False and provider.resource_version == 1

    tool = ProviderInventoryTool(
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash=DESCRIPTOR_HASH,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash="d" * 64,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )
    inventory_hash = provider_inventory_snapshot_hash(
        provider_id=PROVIDER_ID,
        protocol_version="superlily-provider-pull-v1",
        tools=[tool],
    )
    now = datetime.now(timezone.utc).isoformat()
    inventory = await client.post(
        "/v1/provider-inventory/snapshots",
        json={
            "schema_version": "1.0",
            "provider_id": PROVIDER_ID,
            "snapshot_hash": inventory_hash,
            "observed_at": now,
            "protocol_version": "superlily-provider-pull-v1",
            "tools": [tool.model_dump(mode="json")],
        },
        headers={
            "authorization": "Bearer provider-status-secret",
            "idempotency-key": "rollout-runtime-inventory-1",
        },
    )
    assert inventory.status_code == 201, inventory.text
    heartbeat = await client.post(
        "/v1/providers/heartbeats",
        json={
            "schema_version": "1.0",
            "provider_id": PROVIDER_ID,
            "inventory_hash": inventory_hash,
            "observed_at": now,
            "health": "healthy",
            "current_concurrency": 0,
            "max_concurrency": 1,
            "oldest_work_age_ms": None,
            "metadata": {"test": "rollout"},
        },
        headers={"authorization": "Bearer provider-status-secret"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    plan = await _import_plan(
        app,
        _plan_source(
            max_invocations=max_invocations,
            expected_descriptor_version=expected_descriptor_version,
            expected_provider_version=expected_provider_version,
        ),
    )
    return descriptor, plan, inventory_hash


def _preview_payload(plan: ToolRolloutPlanRecord, desired: str) -> dict:
    return {
        "schema_version": "1.0",
        "plan_id": plan.plan_id,
        "version": plan.version,
        "plan_hash": plan.plan_hash,
        "desired_lifecycle": desired,
    }


async def _preview(control, csrf: str, plan: ToolRolloutPlanRecord, desired: str):
    return await control.post(
        "/v1/control/rollout-plans/lifecycle/preview",
        json=_preview_payload(plan, desired),
        headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
    )


async def _apply(control, csrf: str, preview: dict, key: str, *, reason: str):
    payload = {
        **_preview_payload(
            type(
                "PlanRef",
                (),
                {
                    "plan_id": preview["preview"]["target"]["plan_id"],
                    "version": preview["preview"]["target"]["version"],
                    "plan_hash": preview["preview"]["target"]["plan_hash"],
                },
            )(),
            preview["preview"]["after"]["lifecycle"],
        ),
        "preview_id": preview["preview_id"],
        "preview_hash": preview["preview_hash"],
        "expected_version": preview["expected_version"],
        "reason": reason,
    }
    return await control.post(
        "/v1/control/rollout-plans/lifecycle/apply",
        json=payload,
        headers={
            "origin": CONTROL_ORIGIN,
            CONTROL_CSRF_HEADER: csrf,
            "idempotency-key": key,
        },
    )


def _invocation_payload() -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": "status.inspect",
        "descriptor_version": "1.0.0",
        "descriptor_hash": DESCRIPTOR_HASH,
        "input": {"scope": "provider_runtime"},
        "principal": {
            "platform": "qq",
            "sender_id": "123456",
            "conversation_id": "group:1080353942",
            "conversation_type": "group",
            "platform_roles": ["member"],
            "source_event_id": "qq:message:rollout-test",
            "entry_id": "rollout-status-test",
        },
        "capabilities": [],
    }


async def _create_invocation(client, key: str):
    return await client.post(
        "/v1/tool-invocations",
        json=_invocation_payload(),
        headers={
            "authorization": "Bearer admin-secret",
            "idempotency-key": key,
        },
    )


async def _activate_direct(app, plan: ToolRolloutPlanRecord) -> None:
    now = datetime.now(timezone.utc)
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolRolloutPlanRecord, plan.id)
        assert stored is not None
        session.add(
            ToolRolloutPlanLifecycleEvent(
                plan_record_id=stored.id,
                sequence=stored.resource_version + 1,
                previous_lifecycle=stored.lifecycle,
                lifecycle="active",
                actor="test-operator",
                reason="direct test activation with matching event",
            )
        )
        await session.flush()
        await session.execute(
            update(ToolRolloutPlanRecord)
            .where(ToolRolloutPlanRecord.id == stored.id)
            .values(
                lifecycle="active",
                resource_version=stored.resource_version + 1,
                updated_at=now,
            )
        )
        await session.commit()


async def test_rollout_import_is_reviewed_idempotent_and_database_immutable(app) -> None:
    source = _plan_source()
    digest = load_tool_rollout_plan(source).authority.sha256
    plan = await _import_plan(app, source)
    async with app.state.database.sessions() as session:
        duplicate, repeated = await import_tool_rollout_plan(
            session,
            source,
            source_commit="4" * 40,
            bundle_hash=digest,
            reviewer="rollout-git-reviewer",
        )
        items = list(
            (
                await session.scalars(
                    select(ToolRolloutPlanItemRecord).where(
                        ToolRolloutPlanItemRecord.plan_record_id == plan.id
                    )
                )
            ).all()
        )
        events = list(
            (
                await session.scalars(
                    select(ToolRolloutPlanLifecycleEvent).where(
                        ToolRolloutPlanLifecycleEvent.plan_record_id == plan.id
                    )
                )
            ).all()
        )
        counter = await session.get(ToolRolloutPlanCounter, plan.id)
    assert repeated is True and duplicate.id == plan.id
    assert plan.lifecycle == "reviewed" and plan.resource_version == 1
    assert len(items) == 1 and items[0].provider_id == PROVIDER_ID
    assert [(item.sequence, item.lifecycle) for item in events] == [(1, "reviewed")]
    assert counter is not None and counter.consumed_invocations == 0

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolRolloutPlanRecord)
                .where(ToolRolloutPlanRecord.id == plan.id)
                .values(plan_hash="f" * 64)
            )
            await session.commit()
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                delete(ToolRolloutPlanItemRecord).where(
                    ToolRolloutPlanItemRecord.plan_record_id == plan.id
                )
            )
            await session.commit()
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolRolloutPlanRecord)
                .where(ToolRolloutPlanRecord.id == plan.id)
                .values(lifecycle="active", resource_version=2)
            )
            await session.commit()
        await session.rollback()
        await session.execute(
            update(ToolRolloutPlanCounter)
            .where(ToolRolloutPlanCounter.plan_record_id == plan.id)
            .values(
                consumed_invocations=1,
                last_consumed_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        with pytest.raises(DBAPIError):
            await session.execute(
                update(ToolRolloutPlanCounter)
                .where(ToolRolloutPlanCounter.plan_record_id == plan.id)
                .values(consumed_invocations=0)
            )
            await session.commit()


async def test_rollout_admin_import_reads_git_bound_authority(
    tmp_path,
    monkeypatch,
) -> None:
    source = _plan_source()
    digest = load_tool_rollout_plan(source).authority.sha256
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'rollout-admin.db'}"
    database = Database(database_url)
    await database.create_schema()
    await database.dispose()
    monkeypatch.setenv("SUPERLILY_DATABASE_URL", database_url)
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "ledger_only")
    monkeypatch.setattr(
        tool_registry_admin,
        "_git_authority_source",
        lambda *_args: source,
    )

    result = await tool_registry_admin._run(
        SimpleNamespace(
            command="import-rollout-plan",
            repository=tmp_path,
            source_commit="4" * 40,
            path=Path("registry/rollouts/status-canary.json"),
            bundle_hash=digest,
            reviewer="rollout-admin-reviewer",
        )
    )

    assert result == {
        "duplicate": False,
        "execution_ceiling": "ledger_only",
        "lifecycle": "reviewed",
        "plan_hash": digest,
        "plan_id": "status-inspect-controlled-canary",
        "version": "1.0.0",
    }


async def test_operator_activation_limit_pause_and_idempotency(app, client) -> None:
    _enable_control(app)
    _, plan, inventory_hash = await _setup_authority(app, client, max_invocations=1)
    async with _control_client(app) as control:
        csrf = await _login(control)
        activation_response = await _preview(control, csrf, plan, "active")
        assert activation_response.status_code == 200, activation_response.text
        activation = activation_response.json()
        assert activation["preview"]["transition"] == {
            "allowed": True,
            "blockers": [],
            "authority_change": "increase",
        }
        applied = await _apply(
            control,
            csrf,
            activation,
            "rollout-activate-1",
            reason="Activate one reviewed status canary",
        )
        assert applied.status_code == 200, applied.text
        replay = await _apply(
            control,
            csrf,
            activation,
            "rollout-activate-1",
            reason="Activate one reviewed status canary",
        )
        assert replay.status_code == 200 and replay.json()["duplicate"] is True

        first = await _create_invocation(client, "rollout-invocation-1")
        second = await _create_invocation(client, "rollout-invocation-2")
        assert first.status_code == 201 and first.json()["state"] == "queued"
        assert first.json()["policy"]["rollout_plan"]["plan_hash"] == plan.plan_hash
        assert second.status_code == 201 and second.json()["state"] == "recorded_only"
        assert second.json()["reason_code"] == "rollout_fallback_ledger_only"
        assert second.json()["policy"]["rollout_fallback_reasons"] == [
            "rollout_invocation_limit_exhausted"
        ]

        pause_response = await _preview(control, csrf, plan, "paused")
        assert pause_response.status_code == 200, pause_response.text
        pause = pause_response.json()
        assert pause["preview"]["transition"]["allowed"] is True
        paused = await _apply(
            control,
            csrf,
            pause,
            "rollout-pause-1",
            reason="Pause canary after the bounded invocation",
        )
        assert paused.status_code == 200, paused.text

        after_pause = await _create_invocation(client, "rollout-invocation-after-pause")
        assert after_pause.status_code == 201
        assert after_pause.json()["state"] == "recorded_only"
        assert after_pause.json()["reason_code"] == "rollout_fallback_ledger_only"
        assert after_pause.json()["policy"]["rollout_fallback_reasons"] == [
            "reviewed_rollout_plan_unavailable"
        ]

    lease = await client.post(
        "/v1/tool-executions/lease",
        json={"schema_version": "1.0", "inventory_hash": inventory_hash},
        headers={"authorization": "Bearer provider-status-secret"},
    )
    assert lease.status_code == 204
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolRolloutPlanRecord, plan.id)
        counter = await session.get(ToolRolloutPlanCounter, plan.id)
        events = list(
            (
                await session.scalars(
                    select(ToolRolloutPlanLifecycleEvent)
                    .where(ToolRolloutPlanLifecycleEvent.plan_record_id == plan.id)
                    .order_by(ToolRolloutPlanLifecycleEvent.sequence)
                )
            ).all()
        )
        mutations = int(await session.scalar(select(func.count(ControlPlaneMutation.id))) or 0)
        audits = list((await session.scalars(select(ControlPlaneAuditEvent))).all())
    assert stored is not None and stored.lifecycle == "paused" and stored.resource_version == 3
    assert counter is not None and counter.consumed_invocations == 1
    assert [(item.sequence, item.lifecycle) for item in events] == [
        (1, "reviewed"),
        (2, "active"),
        (3, "paused"),
    ]
    assert mutations == 2
    audit_text = json.dumps([item.evidence_json for item in audits], ensure_ascii=False)
    assert CONTROL_PASSWORD not in audit_text
    assert "provider-status-secret" not in audit_text


async def test_rollout_mutation_is_disabled_without_operator(app, client) -> None:
    plan = await _import_plan(app, _plan_source())
    response = await client.post(
        "/v1/control/rollout-plans/lifecycle/preview",
        json=_preview_payload(plan, "active"),
        headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: "x" * 43},
    )
    assert response.status_code == 503
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolRolloutPlanRecord, plan.id)
        mutation_count = int(
            await session.scalar(select(func.count(ControlPlaneMutation.id))) or 0
        )
    assert stored is not None and stored.lifecycle == "reviewed"
    assert mutation_count == 0


async def test_rollout_apply_recomputes_environment_and_runtime_drift(app, client) -> None:
    _enable_control(app)
    _, plan, _ = await _setup_authority(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview_response = await _preview(control, csrf, plan, "active")
        assert preview_response.status_code == 200
        preview = preview_response.json()
        app.state.settings = replace(app.state.settings, tool_global_stop=True)
        applied = await _apply(
            control,
            csrf,
            preview,
            "rollout-stale-preview-1",
            reason="Reject activation after emergency stop changed",
        )
        replay = await _apply(
            control,
            csrf,
            preview,
            "rollout-stale-preview-1",
            reason="Reject activation after emergency stop changed",
        )
    assert applied.status_code == 409
    assert applied.json()["reason_code"] == "preview_stale"
    assert replay.status_code == 409 and replay.json()["duplicate"] is True
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolRolloutPlanRecord, plan.id)
    assert stored is not None and stored.lifecycle == "reviewed"


async def test_pause_tolerates_runtime_counter_drift_after_preview(app, client) -> None:
    _enable_control(app)
    _, plan, _ = await _setup_authority(app, client, max_invocations=2)
    await _activate_direct(app, plan)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview_response = await _preview(control, csrf, plan, "paused")
        assert preview_response.status_code == 200
        preview = preview_response.json()

        queued = await _create_invocation(client, "rollout-pause-drift-invocation-1")
        assert queued.status_code == 201 and queued.json()["state"] == "queued"

        applied = await _apply(
            control,
            csrf,
            preview,
            "rollout-pause-runtime-drift-1",
            reason="Pause must win despite runtime counter drift",
        )

    assert applied.status_code == 200, applied.text
    assert applied.json()["preview_hash"] == preview["preview_hash"]
    assert applied.json()["recomputed_preview_hash"] != preview["preview_hash"]
    assert applied.json()["runtime_drift_tolerated"] is True
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolRolloutPlanRecord, plan.id)
    assert stored is not None and stored.lifecycle == "paused"


async def test_activation_preview_fails_closed_under_ledger_only(app, client) -> None:
    _enable_control(app, mode="ledger_only")
    _, plan, _ = await _setup_authority(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        response = await _preview(control, csrf, plan, "active")
    assert response.status_code == 200, response.text
    assert response.json()["preview"]["transition"]["allowed"] is False
    assert "execution_ceiling_not_canary" in response.json()["preview"]["transition"]["blockers"]


async def test_activation_preview_fails_closed_on_resource_version_drift(app, client) -> None:
    _enable_control(app)
    _, plan, _ = await _setup_authority(
        app,
        client,
        expected_descriptor_version=3,
        expected_provider_version=2,
    )
    async with _control_client(app) as control:
        csrf = await _login(control)
        response = await _preview(control, csrf, plan, "active")

    assert response.status_code == 200, response.text
    preview = response.json()["preview"]
    assert preview["transition"]["allowed"] is False
    assert "status-inspect-admin:descriptor_resource_version_mismatch" in preview[
        "transition"
    ]["blockers"]
    assert "status-inspect-admin:provider_resource_version_mismatch" in preview[
        "transition"
    ]["blockers"]


@pytest.mark.parametrize("role", ["reviewer", "security_admin", "auditor"])
async def test_non_operator_roles_cannot_activate_rollout(app, client, role: str) -> None:
    _enable_control(app, role=role)
    _, plan, _ = await _setup_authority(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        response = await _preview(control, csrf, plan, "active")
    assert response.status_code == 403


async def test_break_glass_can_pause_but_cannot_activate(app, client) -> None:
    _enable_control(app, role="break_glass")
    _, plan, _ = await _setup_authority(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        forbidden = await _preview(control, csrf, plan, "active")
        assert forbidden.status_code == 403
    await _activate_direct(app, plan)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview_response = await _preview(control, csrf, plan, "paused")
        assert preview_response.status_code == 200, preview_response.text
        applied = await _apply(
            control,
            csrf,
            preview_response.json(),
            "rollout-break-glass-pause-1",
            reason="Emergency pause of reviewed rollout plan",
        )
    assert applied.status_code == 200, applied.text


async def test_postgres_plan_lock_closes_pause_lease_race(app, client) -> None:
    if app.state.database.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL plan row-lock ordering is verified on PostgreSQL")
    _enable_control(app)
    descriptor, plan, inventory_hash = await _setup_authority(app, client)
    await _activate_direct(app, plan)
    queued = await _create_invocation(client, "rollout-plan-lock-invocation-1")
    assert queued.status_code == 201 and queued.json()["state"] == "queued"

    async with app.state.database.sessions() as holder:
        locked = await holder.scalar(
            select(ToolRolloutPlanRecord)
            .where(ToolRolloutPlanRecord.id == plan.id)
            .with_for_update()
        )
        assert locked is not None
        pending_lease = asyncio.create_task(
            client.post(
                "/v1/tool-executions/lease",
                json={"schema_version": "1.0", "inventory_hash": inventory_hash},
                headers={"authorization": "Bearer provider-status-secret"},
            )
        )
        await asyncio.sleep(0.05)
        assert pending_lease.done() is False
        next_version = locked.resource_version + 1
        holder.add(
            ToolRolloutPlanLifecycleEvent(
                plan_record_id=locked.id,
                sequence=next_version,
                previous_lifecycle=locked.lifecycle,
                lifecycle="paused",
                actor="test-operator",
                reason="concurrent rollout pause row-lock test",
            )
        )
        await holder.flush()
        await holder.execute(
            update(ToolRolloutPlanRecord)
            .where(
                ToolRolloutPlanRecord.id == locked.id,
                ToolRolloutPlanRecord.resource_version == locked.resource_version,
            )
            .values(
                lifecycle="paused",
                resource_version=next_version,
                updated_at=datetime.now(timezone.utc),
            )
        )
        await holder.commit()

    lease = await pending_lease
    assert lease.status_code == 204
    async with app.state.database.sessions() as session:
        attempts = int(await session.scalar(select(func.count(ToolAttempt.id))) or 0)
        invocation = await session.get(ToolInvocation, queued.json()["invocation_id"])
    assert attempts == 0
    assert invocation is not None and invocation.state == "queued"


async def test_postgres_max_invocation_counter_has_one_queue_winner(app, client) -> None:
    if app.state.database.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL concurrent plan consumption is verified on PostgreSQL")
    _enable_control(app)
    _, plan, _ = await _setup_authority(app, client, max_invocations=1)
    await _activate_direct(app, plan)

    first, second = await asyncio.gather(
        _create_invocation(client, "rollout-counter-race-1"),
        _create_invocation(client, "rollout-counter-race-2"),
    )
    assert first.status_code == 201 and second.status_code == 201
    assert sorted([first.json()["state"], second.json()["state"]]) == [
        "queued",
        "recorded_only",
    ]
    fallback = first.json() if first.json()["state"] == "recorded_only" else second.json()
    assert fallback["reason_code"] == "rollout_fallback_ledger_only"
    async with app.state.database.sessions() as session:
        counter = await session.get(ToolRolloutPlanCounter, plan.id)
    assert counter is not None and counter.consumed_invocations == 1
