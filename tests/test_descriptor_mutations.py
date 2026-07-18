from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
)
from superlily_core.control_plane import (
    CONTROL_CSRF_HEADER,
    hash_control_password,
)
from superlily_core.models import (
    ControlPlaneAuditEvent,
    ControlPlaneMutation,
    ControlPlanePreview,
    ControlPlaneSession,
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
)
from superlily_core.settings import ControlOperator
from superlily_core.tool_registry_service import (
    import_tool_descriptor,
    register_tool_provider,
)


VECTOR_ROOT = Path(__file__).parents[1] / "packages/contracts/vectors/tool_registry"
DESCRIPTOR_SOURCE = (VECTOR_ROOT / "status.inspect-1.0.0.json").read_bytes()
DESCRIPTOR_HASH = load_tool_descriptor(DESCRIPTOR_SOURCE).authority.sha256
CONTROL_PASSWORD = "correct horse battery staple"
CONTROL_PASSWORD_HASH = hash_control_password(CONTROL_PASSWORD, salt=b"fedcba9876543210")
CONTROL_ORIGIN = "https://control.test"


def _enable_control(app, *, role: str = "reviewer", mutation_attempts: int = 10) -> None:
    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode="ledger_only",
        control_operators={
            "control.user": ControlOperator(
                operator_id="control.user",
                role=role,
                password_hash=CONTROL_PASSWORD_HASH,
            )
        },
        control_allowed_hosts=frozenset({"control.test"}),
        control_allowed_origins=frozenset({CONTROL_ORIGIN}),
        control_audit_pepper="descriptor-mutation-test-pepper-32-bytes",
        control_mutation_attempts=mutation_attempts,
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
            "operator_id": "control.user",
            "password": CONTROL_PASSWORD,
        },
        headers={"origin": CONTROL_ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


async def _setup_healthy_tool(app, client: httpx.AsyncClient) -> None:
    async with app.state.database.sessions() as session:
        descriptor, duplicate = await import_tool_descriptor(
            session,
            DESCRIPTOR_SOURCE,
            source_commit="1" * 40,
            bundle_hash=DESCRIPTOR_HASH,
            reviewer="git-reviewer",
        )
    assert duplicate is False
    assert descriptor.resource_version == 1
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
            actor="git-reviewer",
            settings=app.state.settings,
        )
    assert duplicate is False

    tool = ProviderInventoryTool(
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash=DESCRIPTOR_HASH,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash="a" * 64,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )
    snapshot_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[tool],
    )
    inventory = await client.post(
        "/v1/provider-inventory/snapshots",
        json={
            "schema_version": "1.0",
            "provider_id": "provider-status-primary",
            "snapshot_hash": snapshot_hash,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "protocol_version": "superlily-provider-pull-v1",
            "tools": [tool.model_dump(mode="json")],
        },
        headers={
            "authorization": "Bearer provider-status-secret",
            "idempotency-key": "descriptor-mutation-inventory",
        },
    )
    assert inventory.status_code == 201, inventory.text
    heartbeat = await client.post(
        "/v1/providers/heartbeats",
        json={
            "schema_version": "1.0",
            "provider_id": "provider-status-primary",
            "inventory_hash": snapshot_hash,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "health": "healthy",
            "current_concurrency": 0,
            "max_concurrency": 1,
            "oldest_work_age_ms": None,
            "metadata": {"implementation": "descriptor-mutation-test"},
        },
        headers={"authorization": "Bearer provider-status-secret"},
    )
    assert heartbeat.status_code == 200, heartbeat.text


def _preview_payload(desired_lifecycle: str) -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": "status.inspect",
        "descriptor_version": "1.0.0",
        "descriptor_hash": DESCRIPTOR_HASH,
        "desired_lifecycle": desired_lifecycle,
    }


def _apply_payload(preview: dict, *, reason: str = "reviewed lifecycle test") -> dict:
    return {
        **_preview_payload(preview["preview"]["after"]["lifecycle"]),
        "preview_id": preview["preview_id"],
        "preview_hash": preview["preview_hash"],
        "expected_version": preview["expected_version"],
        "reason": reason,
    }


async def _preview(
    client: httpx.AsyncClient,
    csrf: str,
    desired_lifecycle: str,
) -> httpx.Response:
    return await client.post(
        "/v1/control/descriptors/lifecycle/preview",
        json=_preview_payload(desired_lifecycle),
        headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
    )


async def _apply(
    client: httpx.AsyncClient,
    csrf: str,
    preview: dict,
    idempotency_key: str,
    *,
    reason: str = "reviewed lifecycle test",
) -> httpx.Response:
    return await client.post(
        "/v1/control/descriptors/lifecycle/apply",
        json=_apply_payload(preview, reason=reason),
        headers={
            "origin": CONTROL_ORIGIN,
            CONTROL_CSRF_HEADER: csrf,
            "idempotency-key": idempotency_key,
        },
    )


async def test_descriptor_mutation_is_disabled_without_control_operator(app, client) -> None:
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        response = await control.post(
            "/v1/control/descriptors/lifecycle/preview",
            json=_preview_payload("active"),
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: "x" * 43},
        )

    assert response.status_code == 503
    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ToolDescriptorRecord))
    assert record is not None and record.lifecycle == "reviewed"


async def test_descriptor_activation_suspend_restore_and_idempotency(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        activation = await _preview(control, csrf, "active")
        assert activation.status_code == 200, activation.text
        activation_preview = activation.json()
        assert activation_preview["expected_version"] == 1
        assert activation_preview["preview"]["transition"] == {
            "allowed": True,
            "blockers": [],
            "authority_change": "increase",
        }
        assert activation_preview["preview"]["before"]["lifecycle"] == "reviewed"
        assert activation_preview["preview"]["after"]["lifecycle"] == "active"

        missing_csrf = await control.post(
            "/v1/control/descriptors/lifecycle/apply",
            json=_apply_payload(activation_preview),
            headers={"origin": CONTROL_ORIGIN, "idempotency-key": "activate-missing-csrf"},
        )
        assert missing_csrf.status_code == 403

        activated = await _apply(
            control,
            csrf,
            activation_preview,
            "activate-status-1",
        )
        assert activated.status_code == 200, activated.text
        assert activated.json()["duplicate"] is False
        assert activated.json()["resource_version"] == 2
        replay = await _apply(control, csrf, activation_preview, "activate-status-1")
        assert replay.status_code == 200
        assert replay.json()["duplicate"] is True
        conflict = await _apply(
            control,
            csrf,
            activation_preview,
            "activate-status-1",
            reason="different reviewed reason",
        )
        assert conflict.status_code == 409

        suspension = await _preview(control, csrf, "suspended")
        assert suspension.status_code == 200
        suspended = await _apply(control, csrf, suspension.json(), "suspend-status-1")
        assert suspended.status_code == 200
        assert suspended.json()["resource_version"] == 3

        restoration = await _preview(control, csrf, "active")
        assert restoration.status_code == 200
        restored = await _apply(control, csrf, restoration.json(), "restore-status-1")
        assert restored.status_code == 200
        assert restored.json()["resource_version"] == 4

    async with app.state.database.sessions() as session:
        descriptor = await session.scalar(select(ToolDescriptorRecord))
        events = (
            await session.scalars(
                select(ToolDescriptorLifecycleEvent).order_by(
                    ToolDescriptorLifecycleEvent.sequence
                )
            )
        ).all()
        mutations = (
            await session.scalars(
                select(ControlPlaneMutation).order_by(ControlPlaneMutation.created_at)
            )
        ).all()
        previews = (await session.scalars(select(ControlPlanePreview))).all()

    assert descriptor is not None
    assert (descriptor.lifecycle, descriptor.resource_version) == ("active", 4)
    assert [(item.sequence, item.previous_lifecycle, item.lifecycle) for item in events] == [
        (1, None, "reviewed"),
        (2, "reviewed", "active"),
        (3, "active", "suspended"),
        (4, "suspended", "active"),
    ]
    assert [item.outcome for item in mutations] == ["accepted", "accepted", "accepted"]
    assert len(previews) == 3
    stored = json.dumps(
        [item.result_json for item in mutations]
        + [item.preview_json for item in previews],
        sort_keys=True,
    )
    assert CONTROL_PASSWORD not in stored
    assert csrf not in stored


async def test_role_fresh_reauthentication_and_preview_expiry_are_enforced(app, client) -> None:
    _enable_control(app, role="auditor")
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        forbidden = await _preview(control, csrf, "active")
        assert forbidden.status_code == 403

    _enable_control(app, role="reviewer")
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview_response = await _preview(control, csrf, "active")
        assert preview_response.status_code == 200
        preview = preview_response.json()

        async with app.state.database.sessions() as session:
            session_record = await session.scalar(
                select(ControlPlaneSession).where(ControlPlaneSession.role == "reviewer")
            )
            stored_preview = await session.get(ControlPlanePreview, preview["preview_id"])
            assert session_record is not None and stored_preview is not None
            session_record.last_reauthenticated_at = (
                session_record.last_reauthenticated_at - timedelta(hours=1)
            )
            await session.commit()

        stale_session = await _apply(control, csrf, preview, "stale-reauth-1")
        assert stale_session.status_code == 403

        async with app.state.database.sessions() as session:
            session_record = await session.get(ControlPlaneSession, session_record.id)
            assert session_record is not None
            session_record.last_reauthenticated_at = datetime.now(timezone.utc)
            expired_preview = ControlPlanePreview(
                id="00000000-0000-4000-8000-000000000001",
                session_id=session_record.id,
                operator_id="control.user",
                role="reviewer",
                operation=stored_preview.operation,
                target_type=stored_preview.target_type,
                target_id=stored_preview.target_id,
                request_hash=stored_preview.request_hash,
                expected_version=stored_preview.expected_version,
                preview_json=stored_preview.preview_json,
                preview_hash=stored_preview.preview_hash,
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
            session.add(expired_preview)
            await session.commit()
        expired_payload = {**preview, "preview_id": expired_preview.id}
        expired = await _apply(control, csrf, expired_payload, "expired-preview-1")
        assert expired.status_code == 409
        assert expired.json()["reason_code"] == "preview_expired"


async def test_runtime_drift_and_non_applicable_preview_fail_closed(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "active")).json()
        app.state.settings = replace(app.state.settings, tool_global_stop=True)

        stale = await _apply(control, csrf, preview, "runtime-drift-1")
        assert stale.status_code == 409
        assert stale.json()["reason_code"] == "preview_stale"
        replay = await _apply(control, csrf, preview, "runtime-drift-1")
        assert replay.status_code == 409
        assert replay.json()["duplicate"] is True

        blocked_preview = await _preview(control, csrf, "active")
        assert blocked_preview.status_code == 200
        assert blocked_preview.json()["preview"]["transition"]["allowed"] is False
        assert "global_stop" in blocked_preview.json()["preview"]["transition"]["blockers"]
        blocked = await _apply(control, csrf, blocked_preview.json(), "blocked-preview-1")
        assert blocked.status_code == 409
        assert blocked.json()["reason_code"] == "preview_not_applicable"

    async with app.state.database.sessions() as session:
        descriptor = await session.scalar(select(ToolDescriptorRecord))
        rejected = (await session.scalars(select(ControlPlaneMutation))).all()
    assert descriptor is not None and descriptor.lifecycle == "reviewed"
    assert [item.outcome for item in rejected] == ["rejected", "rejected"]


async def test_activation_requires_eligible_provider_and_preview_rate_is_bounded(app) -> None:
    _enable_control(app, mutation_attempts=1)
    async with app.state.database.sessions() as session:
        _, duplicate = await import_tool_descriptor(
            session,
            DESCRIPTOR_SOURCE,
            source_commit="1" * 40,
            bundle_hash=DESCRIPTOR_HASH,
            reviewer="git-reviewer",
        )
    assert duplicate is False
    async with _control_client(app) as control:
        csrf = await _login(control)
        first = await _preview(control, csrf, "active")
        assert first.status_code == 200
        assert first.json()["preview"]["transition"]["allowed"] is False
        assert first.json()["preview"]["transition"]["blockers"] == ["provider_missing"]
        rejected = await _apply(control, csrf, first.json(), "provider-missing-1")
        assert rejected.status_code == 409
        assert rejected.json()["reason_code"] == "preview_not_applicable"

        assert (await _preview(control, csrf, "active")).status_code == 200
        assert (await _preview(control, csrf, "active")).status_code == 200
        limited = await _preview(control, csrf, "active")
        assert limited.status_code == 429

    async with app.state.database.sessions() as session:
        descriptor = await session.scalar(select(ToolDescriptorRecord))
    assert descriptor is not None and descriptor.lifecycle == "reviewed"


async def test_concurrent_apply_has_one_cas_winner(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "active")).json()
        results = await asyncio.gather(
            _apply(control, csrf, preview, "concurrent-apply-1"),
            _apply(control, csrf, preview, "concurrent-apply-2"),
        )

    assert sorted(item.status_code for item in results) == [200, 409]
    async with app.state.database.sessions() as session:
        descriptor = await session.scalar(select(ToolDescriptorRecord))
        events = (await session.scalars(select(ToolDescriptorLifecycleEvent))).all()
        mutations = (await session.scalars(select(ControlPlaneMutation))).all()
    assert descriptor is not None
    assert (descriptor.lifecycle, descriptor.resource_version) == ("active", 2)
    assert len(events) == 2
    assert sorted(item.outcome for item in mutations) == ["accepted", "rejected"]


async def test_descriptor_authority_and_m1_evidence_are_immutable(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "active")).json()
        applied = await _apply(control, csrf, preview, "immutable-evidence-1")
        assert applied.status_code == 200

    statements = [
        update(ToolDescriptorRecord).values(tool_id="tampered.tool"),
        update(ToolDescriptorRecord).values(
            lifecycle="suspended",
            resource_version=3,
        ),
        delete(ToolDescriptorRecord),
        update(ToolDescriptorLifecycleEvent).values(reason="tampered"),
        delete(ToolDescriptorLifecycleEvent),
        update(ControlPlanePreview).values(preview_hash="f" * 64),
        delete(ControlPlanePreview),
    ]
    for statement in statements:
        async with app.state.database.sessions() as session:
            with pytest.raises(DBAPIError):
                await session.execute(statement)
                await session.commit()
            await session.rollback()


async def test_descriptor_mutation_rate_limit_is_independent_of_login_limit(app, client) -> None:
    _enable_control(app, mutation_attempts=1)
    await _setup_healthy_tool(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        activation = (await _preview(control, csrf, "active")).json()
        assert (await _apply(control, csrf, activation, "rate-limit-apply-1")).status_code == 200
        suspension = (await _preview(control, csrf, "suspended")).json()
        limited = await _apply(control, csrf, suspension, "rate-limit-apply-2")

    assert limited.status_code == 429
    async with app.state.database.sessions() as session:
        descriptor = await session.scalar(select(ToolDescriptorRecord))
    assert descriptor is not None and descriptor.lifecycle == "active"
