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
from superlily_core.control_plane import CONTROL_CSRF_HEADER, hash_control_password
from superlily_core.models import (
    ControlPlaneMutation,
    ControlPlanePreview,
    ControlPlaneSession,
    ToolProvider,
    ToolProviderLifecycleEvent,
)
from superlily_core.settings import ControlOperator
from superlily_core.tool_registry_service import (
    import_tool_descriptor,
    register_tool_provider,
)


VECTOR_ROOT = Path(__file__).parents[1] / "packages/contracts/vectors/tool_registry"
DESCRIPTOR_SOURCE = (VECTOR_ROOT / "status.inspect-1.0.0.json").read_bytes()
DESCRIPTOR_HASH = load_tool_descriptor(DESCRIPTOR_SOURCE).authority.sha256
PROVIDER_ID = "provider-status-primary"
CONTROL_PASSWORD = "correct horse battery staple"
CONTROL_PASSWORD_HASH = hash_control_password(CONTROL_PASSWORD, salt=b"0123456789abcdef")
CONTROL_ORIGIN = "https://control.test"


def _enable_control(
    app,
    *,
    role: str = "security_admin",
    mutation_attempts: int = 10,
) -> None:
    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode="ledger_only",
        control_operators={
            "security.user": ControlOperator(
                operator_id="security.user",
                role=role,
                password_hash=CONTROL_PASSWORD_HASH,
            )
        },
        control_allowed_hosts=frozenset({"control.test"}),
        control_allowed_origins=frozenset({CONTROL_ORIGIN}),
        control_audit_pepper="provider-mutation-test-pepper-32-bytes",
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
            "operator_id": "security.user",
            "password": CONTROL_PASSWORD,
        },
        headers={"origin": CONTROL_ORIGIN},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


async def _publish_runtime(
    client: httpx.AsyncClient,
    *,
    suffix: str,
    health: str = "healthy",
) -> str:
    tool = ProviderInventoryTool(
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash=DESCRIPTOR_HASH,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash="b" * 64,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )
    snapshot_hash = provider_inventory_snapshot_hash(
        provider_id=PROVIDER_ID,
        protocol_version="superlily-provider-pull-v1",
        tools=[tool],
    )
    inventory = await client.post(
        "/v1/provider-inventory/snapshots",
        json={
            "schema_version": "1.0",
            "provider_id": PROVIDER_ID,
            "snapshot_hash": snapshot_hash,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "protocol_version": "superlily-provider-pull-v1",
            "tools": [tool.model_dump(mode="json")],
        },
        headers={
            "authorization": "Bearer provider-status-secret",
            "idempotency-key": f"provider-mutation-inventory-{suffix}",
        },
    )
    assert inventory.status_code in {200, 201}, inventory.text
    heartbeat = await client.post(
        "/v1/providers/heartbeats",
        json={
            "schema_version": "1.0",
            "provider_id": PROVIDER_ID,
            "inventory_hash": snapshot_hash,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "health": health,
            "current_concurrency": 0,
            "max_concurrency": 1,
            "oldest_work_age_ms": None,
            "metadata": {"implementation": "provider-mutation-test"},
        },
        headers={"authorization": "Bearer provider-status-secret"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    return snapshot_hash


async def _setup_healthy_provider(app, client: httpx.AsyncClient) -> None:
    async with app.state.database.sessions() as session:
        await import_tool_descriptor(
            session,
            DESCRIPTOR_SOURCE,
            source_commit="2" * 40,
            bundle_hash=DESCRIPTOR_HASH,
            reviewer="git-reviewer",
        )
        provider, duplicate = await register_tool_provider(
            session,
            ProviderRegistration(
                provider_id=PROVIDER_ID,
                owner="superlily-operations",
                lifecycle="active",
                allowed_protocols=["superlily-provider-pull-v1"],
                tool_selectors=["status.inspect"],
            ),
            actor="git-reviewer",
            settings=app.state.settings,
        )
    assert duplicate is False
    assert provider.resource_version == 1
    await _publish_runtime(client, suffix="initial")


def _preview_payload(desired_lifecycle: str) -> dict:
    return {
        "schema_version": "1.0",
        "provider_id": PROVIDER_ID,
        "desired_lifecycle": desired_lifecycle,
    }


def _apply_payload(preview: dict, *, reason: str = "reviewed provider lifecycle test") -> dict:
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
        "/v1/control/providers/lifecycle/preview",
        json=_preview_payload(desired_lifecycle),
        headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
    )


async def _apply(
    client: httpx.AsyncClient,
    csrf: str,
    preview: dict,
    idempotency_key: str,
    *,
    reason: str = "reviewed provider lifecycle test",
) -> httpx.Response:
    return await client.post(
        "/v1/control/providers/lifecycle/apply",
        json=_apply_payload(preview, reason=reason),
        headers={
            "origin": CONTROL_ORIGIN,
            CONTROL_CSRF_HEADER: csrf,
            "idempotency-key": idempotency_key,
        },
    )


async def test_provider_mutation_is_disabled_without_control_operator(app, client) -> None:
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        response = await control.post(
            "/v1/control/providers/lifecycle/preview",
            json=_preview_payload("quarantined"),
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: "x" * 43},
        )
    assert response.status_code == 503
    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, PROVIDER_ID)
    assert provider is not None and provider.lifecycle == "active"


async def test_quarantine_restore_reporting_and_idempotency(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        quarantine_response = await _preview(control, csrf, "quarantined")
        assert quarantine_response.status_code == 200, quarantine_response.text
        quarantine = quarantine_response.json()
        assert quarantine["preview"]["transition"] == {
            "allowed": True,
            "blockers": [],
            "authority_change": "decrease",
        }
        assert quarantine["preview"]["affected_tools"][0]["provider_after"] == {
            "runtime_eligible": False,
            "reasons": ["provider_quarantined"],
        }
        missing_csrf = await control.post(
            "/v1/control/providers/lifecycle/apply",
            json=_apply_payload(quarantine),
            headers={"origin": CONTROL_ORIGIN, "idempotency-key": "provider-no-csrf-1"},
        )
        assert missing_csrf.status_code == 403

        applied = await _apply(control, csrf, quarantine, "provider-quarantine-1")
        assert applied.status_code == 200, applied.text
        assert applied.json()["resource_version"] == 2
        replay = await _apply(control, csrf, quarantine, "provider-quarantine-1")
        assert replay.status_code == 200 and replay.json()["duplicate"] is True
        conflict = await _apply(
            control,
            csrf,
            quarantine,
            "provider-quarantine-1",
            reason="a different reviewed quarantine reason",
        )
        assert conflict.status_code == 409

        async with app.state.database.sessions() as session:
            registered, duplicate = await register_tool_provider(
                session,
                ProviderRegistration(
                    provider_id=PROVIDER_ID,
                    owner="superlily-operations",
                    lifecycle="active",
                    allowed_protocols=["superlily-provider-pull-v1"],
                    tool_selectors=["status.inspect"],
                ),
                actor="git-reviewer",
                settings=app.state.settings,
            )
        assert duplicate is True
        assert registered.lifecycle == "quarantined"

        # Quarantine 不撤销 Provider credential；新 runtime 是恢复所需证据。
        await _publish_runtime(client, suffix="while-quarantined")
        restoration_response = await _preview(control, csrf, "active")
        assert restoration_response.status_code == 200
        restoration = restoration_response.json()
        assert restoration["preview"]["transition"]["allowed"] is True
        restored = await _apply(control, csrf, restoration, "provider-restore-1")
        assert restored.status_code == 200, restored.text
        assert restored.json()["resource_version"] == 3

    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, PROVIDER_ID)
        events = (
            await session.scalars(
                select(ToolProviderLifecycleEvent).order_by(
                    ToolProviderLifecycleEvent.sequence
                )
            )
        ).all()
        mutations = (await session.scalars(select(ControlPlaneMutation))).all()
        previews = (await session.scalars(select(ControlPlanePreview))).all()
    assert provider is not None
    assert (provider.lifecycle, provider.resource_version) == ("active", 3)
    assert [(event.sequence, event.previous_lifecycle, event.lifecycle) for event in events] == [
        (1, None, "active"),
        (2, "active", "quarantined"),
        (3, "quarantined", "active"),
    ]
    assert [mutation.outcome for mutation in mutations] == ["accepted", "accepted"]
    stored = json.dumps(
        [mutation.result_json for mutation in mutations]
        + [preview.preview_json for preview in previews],
        sort_keys=True,
    )
    assert CONTROL_PASSWORD not in stored
    assert "provider-status-secret" not in stored
    assert csrf not in stored


async def test_security_role_fresh_reauthentication_and_expiry(app, client) -> None:
    _enable_control(app, role="reviewer")
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        assert (await _preview(control, csrf, "quarantined")).status_code == 403

    _enable_control(app)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "quarantined")).json()
        async with app.state.database.sessions() as session:
            session_record = await session.scalar(
                select(ControlPlaneSession).where(
                    ControlPlaneSession.role == "security_admin"
                )
            )
            stored_preview = await session.get(ControlPlanePreview, preview["preview_id"])
            assert session_record is not None and stored_preview is not None
            session_record.last_reauthenticated_at -= timedelta(hours=1)
            await session.commit()
        assert (await _apply(control, csrf, preview, "provider-stale-reauth-1")).status_code == 403

        async with app.state.database.sessions() as session:
            session_record = await session.get(ControlPlaneSession, session_record.id)
            assert session_record is not None
            session_record.last_reauthenticated_at = datetime.now(timezone.utc)
            expired_preview = ControlPlanePreview(
                id="00000000-0000-4000-8000-000000000002",
                session_id=session_record.id,
                operator_id="security.user",
                role="security_admin",
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
        expired = await _apply(control, csrf, expired_payload, "provider-expired-preview-1")
        assert expired.status_code == 409
        assert expired.json()["reason_code"] == "preview_expired"


async def test_restore_runtime_drift_and_blockers_fail_closed(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        quarantine = (await _preview(control, csrf, "quarantined")).json()
        assert (await _apply(control, csrf, quarantine, "provider-runtime-q-1")).status_code == 200
        restore = (await _preview(control, csrf, "active")).json()
        assert restore["preview"]["transition"]["allowed"] is True

        await _publish_runtime(client, suffix="degraded", health="degraded")
        stale = await _apply(control, csrf, restore, "provider-runtime-stale-1")
        assert stale.status_code == 409
        assert stale.json()["reason_code"] == "preview_stale"
        replay = await _apply(control, csrf, restore, "provider-runtime-stale-1")
        assert replay.status_code == 409 and replay.json()["duplicate"] is True

        blocked = (await _preview(control, csrf, "active")).json()
        assert blocked["preview"]["transition"]["allowed"] is False
        assert "provider_unhealthy" in blocked["preview"]["transition"]["blockers"]
        rejected = await _apply(control, csrf, blocked, "provider-runtime-blocked-1")
        assert rejected.status_code == 409
        assert rejected.json()["reason_code"] == "preview_not_applicable"

    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, PROVIDER_ID)
    assert provider is not None and provider.lifecycle == "quarantined"


async def test_concurrent_provider_apply_has_one_cas_winner(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "quarantined")).json()
        results = await asyncio.gather(
            _apply(control, csrf, preview, "provider-concurrent-1"),
            _apply(control, csrf, preview, "provider-concurrent-2"),
        )
    assert sorted(result.status_code for result in results) == [200, 409]
    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, PROVIDER_ID)
        events = (await session.scalars(select(ToolProviderLifecycleEvent))).all()
        mutations = (await session.scalars(select(ControlPlaneMutation))).all()
    assert provider is not None
    assert (provider.lifecycle, provider.resource_version) == ("quarantined", 2)
    assert len(events) == 2
    assert sorted(mutation.outcome for mutation in mutations) == ["accepted", "rejected"]


async def test_provider_authority_and_lifecycle_evidence_are_immutable(app, client) -> None:
    _enable_control(app)
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        preview = (await _preview(control, csrf, "quarantined")).json()
        assert (await _apply(control, csrf, preview, "provider-immutable-1")).status_code == 200

    statements = [
        update(ToolProvider).values(owner="tampered-owner"),
        update(ToolProvider).values(lifecycle="active", resource_version=3),
        delete(ToolProvider),
        update(ToolProviderLifecycleEvent).values(reason="tampered"),
        delete(ToolProviderLifecycleEvent),
    ]
    for statement in statements:
        async with app.state.database.sessions() as session:
            with pytest.raises(DBAPIError):
                await session.execute(statement)
                await session.commit()
            await session.rollback()


async def test_provider_preview_and_mutation_rates_are_bounded(app, client) -> None:
    _enable_control(app, mutation_attempts=1)
    await _setup_healthy_provider(app, client)
    async with _control_client(app) as control:
        csrf = await _login(control)
        quarantine = (await _preview(control, csrf, "quarantined")).json()
        assert (await _apply(control, csrf, quarantine, "provider-rate-apply-1")).status_code == 200
        restoration = (await _preview(control, csrf, "active")).json()
        assert (await _apply(control, csrf, restoration, "provider-rate-apply-2")).status_code == 429

        # 首个 quarantine preview 加上 restore preview 共两条，本 operation 还允许一条。
        assert (await _preview(control, csrf, "active")).status_code == 200
        limited = await _preview(control, csrf, "active")
        assert limited.status_code == 429

    async with app.state.database.sessions() as session:
        provider = await session.get(ToolProvider, PROVIDER_ID)
    assert provider is not None and provider.lifecycle == "quarantined"
