from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
)
from superlily_core.models import (
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    ToolProviderHeartbeat,
    ToolProviderInventoryEntry,
    ToolProviderInventorySnapshot,
)
from superlily_core.tool_registry_service import import_tool_descriptor, register_tool_provider
from superlily_core.tool_registry_admin import _git_descriptor_source


VECTOR_ROOT = Path(__file__).parents[1] / "packages/contracts/vectors/tool_registry"


def _descriptor_source() -> bytes:
    return (VECTOR_ROOT / "status.inspect-1.0.0.json").read_bytes()


def _registration() -> ProviderRegistration:
    return ProviderRegistration(
        provider_id="provider-status-primary",
        owner="superlily-operations",
        lifecycle="active",
        allowed_protocols=["superlily-provider-pull-v1"],
        tool_selectors=["status.inspect"],
    )


async def _import_descriptor(app) -> ToolDescriptorRecord:
    descriptor_hash = load_tool_descriptor(_descriptor_source()).authority.sha256
    async with app.state.database.sessions() as session:
        record, duplicate = await import_tool_descriptor(
            session,
            _descriptor_source(),
            source_commit="1" * 40,
            bundle_hash=descriptor_hash,
            reviewer="phase3-reviewer",
        )
    assert duplicate is False
    return record


async def _register_provider(app) -> None:
    async with app.state.database.sessions() as session:
        _, duplicate = await register_tool_provider(
            session,
            _registration(),
            actor="phase3-reviewer",
            settings=app.state.settings,
        )
    assert duplicate is False


def _inventory_payload(*, implementation_hash: str = "a" * 64) -> dict:
    descriptor = load_tool_descriptor(_descriptor_source())
    tool = ProviderInventoryTool(
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash=descriptor.authority.sha256,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash=implementation_hash,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )
    snapshot_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[tool],
    )
    return {
        "schema_version": "1.0",
        "provider_id": "provider-status-primary",
        "snapshot_hash": snapshot_hash,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "protocol_version": "superlily-provider-pull-v1",
        "tools": [tool.model_dump(mode="json")],
    }


async def test_empty_registry_is_admin_only_and_has_no_execution_authority(client) -> None:
    assert (await client.get("/v1/tools")).status_code == 401
    response = await client.get(
        "/v1/tools",
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "execution": {
            "mode": "off",
            "invocation_endpoints": False,
            "leases_enabled": False,
            "natural_language_callers": False,
        },
        "summary": {
            "descriptors": 0,
            "active_descriptors": 0,
            "eligible_tools": 0,
            "providers": 0,
            "fresh_inventories": 0,
            "healthy_providers": 0,
        },
        "tools": [],
        "providers": [],
    }
    assert (await client.post("/v1/tool-invocations", json={})).status_code == 404
    assert (
        await client.get(
            "/v1/tools/status.inspect",
            headers={"Authorization": "Bearer admin-secret"},
        )
    ).status_code == 404


async def test_descriptor_import_is_immutable_idempotent_and_never_active(app, client) -> None:
    first = await _import_descriptor(app)
    descriptor_hash = load_tool_descriptor(_descriptor_source()).authority.sha256
    async with app.state.database.sessions() as session:
        second, duplicate = await import_tool_descriptor(
            session,
            _descriptor_source(),
            source_commit="1" * 40,
            bundle_hash=descriptor_hash,
            reviewer="phase3-reviewer",
        )
        descriptor_count = await session.scalar(select(func.count(ToolDescriptorRecord.id)))
        lifecycle_events = (
            await session.scalars(select(ToolDescriptorLifecycleEvent))
        ).all()

    assert duplicate is True
    assert second.id == first.id
    assert descriptor_count == 1
    assert first.lifecycle == "reviewed"
    assert first.canonical_json == load_tool_descriptor(_descriptor_source()).authority.canonical_bytes
    assert [(item.sequence, item.previous_lifecycle, item.lifecycle) for item in lifecycle_events] == [
        (1, None, "reviewed")
    ]

    response = await client.get(
        "/v1/tools/status.inspect",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["tool_id"] == "status.inspect"
    assert len(detail["versions"]) == 1
    tool = detail["versions"][0]
    assert tool["desired"]["lifecycle"] == "reviewed"
    assert tool["effective"] == {
        "eligible": False,
        "execution_mode": "off",
        "reasons": ["inactive_descriptor", "provider_missing", "execution_off"],
    }

    async with app.state.database.sessions() as session:
        with pytest.raises(HTTPException, match="different authority metadata") as conflict:
            await import_tool_descriptor(
                session,
                _descriptor_source(),
                source_commit="1" * 40,
                bundle_hash=descriptor_hash,
                reviewer="different-reviewer",
            )
    assert conflict.value.status_code == 409

    version_two = json.loads(_descriptor_source())
    version_two["version"] = "2.0.0"
    version_two_source = json.dumps(version_two, separators=(",", ":")).encode()
    version_two_hash = load_tool_descriptor(version_two_source).authority.sha256
    async with app.state.database.sessions() as session:
        _, duplicate = await import_tool_descriptor(
            session,
            version_two_source,
            source_commit="2" * 40,
            bundle_hash=version_two_hash,
            reviewer="phase3-reviewer",
        )
    assert duplicate is False
    versions = await client.get(
        "/v1/tools/status.inspect",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert [item["version"] for item in versions.json()["versions"]] == ["1.0.0", "2.0.0"]


async def test_provider_auth_inventory_heartbeat_and_effective_state(client, app) -> None:
    await _import_descriptor(app)
    await _register_provider(app)
    changed_registration = _registration().model_copy(update={"owner": "different-owner"})
    async with app.state.database.sessions() as session:
        with pytest.raises(HTTPException, match="different stable registration") as conflict:
            await register_tool_provider(
                session,
                changed_registration,
                actor="phase3-reviewer",
                settings=app.state.settings,
            )
    assert conflict.value.status_code == 409
    inventory = _inventory_payload()
    headers = {
        "Authorization": "Bearer provider-status-secret",
        "Idempotency-Key": "status-inventory-1",
    }

    assert (
        await client.post(
            "/v1/provider-inventory/snapshots",
            json=inventory,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": "status-inventory-1",
            },
        )
    ).status_code == 401
    wrong_identity = {**inventory, "provider_id": "provider-other"}
    wrong_identity["snapshot_hash"] = provider_inventory_snapshot_hash(
        provider_id="provider-other",
        protocol_version="superlily-provider-pull-v1",
        tools=[ProviderInventoryTool.model_validate(wrong_identity["tools"][0])],
    )
    assert (
        await client.post(
            "/v1/provider-inventory/snapshots",
            json=wrong_identity,
            headers=headers,
        )
    ).status_code == 403

    created = await client.post("/v1/provider-inventory/snapshots", json=inventory, headers=headers)
    duplicate = await client.post("/v1/provider-inventory/snapshots", json=inventory, headers=headers)
    assert created.status_code == 201, created.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["snapshot_id"] == created.json()["snapshot_id"]

    unknown_heartbeat = {
        "schema_version": "1.0",
        "provider_id": "provider-status-primary",
        "inventory_hash": "f" * 64,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "health": "healthy",
        "current_concurrency": 0,
        "max_concurrency": 4,
        "metadata": {},
    }
    rejected = await client.post(
        "/v1/providers/heartbeats",
        json=unknown_heartbeat,
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert rejected.status_code == 422

    heartbeat = {
        **unknown_heartbeat,
        "inventory_hash": inventory["snapshot_hash"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"worker_version": "1.0.0"},
    }
    accepted = await client.post(
        "/v1/providers/heartbeats",
        json=heartbeat,
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    replay = await client.post(
        "/v1/providers/heartbeats",
        json=heartbeat,
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert accepted.status_code == 200, accepted.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["duplicate"] is True
    heartbeat_conflict = await client.post(
        "/v1/providers/heartbeats",
        json={**heartbeat, "health": "degraded"},
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert heartbeat_conflict.status_code == 409

    view = await client.get("/v1/tools", headers={"Authorization": "Bearer admin-secret"})
    assert view.status_code == 200
    assert "provider-status-secret" not in view.text
    payload = view.json()
    assert payload["summary"] == {
        "descriptors": 1,
        "active_descriptors": 0,
        "eligible_tools": 0,
        "providers": 1,
        "fresh_inventories": 1,
        "healthy_providers": 1,
    }
    tool = payload["tools"][0]
    assert tool["reported"] == [
        {
            "provider_id": "provider-status-primary",
            "inventory_hash": inventory["snapshot_hash"],
            "heartbeat_health": "healthy",
            "reasons": [],
            "runtime_eligible": True,
        }
    ]
    assert tool["effective"] == {
        "eligible": False,
        "execution_mode": "off",
        "reasons": ["inactive_descriptor", "execution_off"],
    }

    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolProviderInventorySnapshot.id))) == 1
        assert await session.scalar(select(func.count(ToolProviderInventoryEntry.id))) == 1
        assert await session.scalar(select(func.count(ToolProviderHeartbeat.id))) == 1


async def test_inventory_idempotency_conflict_and_descriptor_mismatch_are_visible(client, app) -> None:
    await _import_descriptor(app)
    await _register_provider(app)
    first = _inventory_payload()
    second = _inventory_payload(implementation_hash="b" * 64)
    headers = {
        "Authorization": "Bearer provider-status-secret",
        "Idempotency-Key": "status-inventory-conflict",
    }
    created = await client.post("/v1/provider-inventory/snapshots", json=first, headers=headers)
    assert created.status_code == 201, created.text
    conflict = await client.post("/v1/provider-inventory/snapshots", json=second, headers=headers)
    assert conflict.status_code == 409

    mismatched = _inventory_payload()
    mismatched["tools"][0]["descriptor_hash"] = "f" * 64
    mismatched_tool = ProviderInventoryTool.model_validate(mismatched["tools"][0])
    mismatched["snapshot_hash"] = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[mismatched_tool],
    )
    mismatched["observed_at"] = datetime.now(timezone.utc).isoformat()
    accepted = await client.post(
        "/v1/provider-inventory/snapshots",
        json=mismatched,
        headers={
            "Authorization": "Bearer provider-status-secret",
            "Idempotency-Key": "status-inventory-mismatch",
        },
    )
    assert accepted.status_code == 201
    heartbeat = {
        "schema_version": "1.0",
        "provider_id": "provider-status-primary",
        "inventory_hash": mismatched["snapshot_hash"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "health": "healthy",
        "current_concurrency": 0,
        "max_concurrency": 4,
        "metadata": {},
    }
    assert (
        await client.post(
            "/v1/providers/heartbeats",
            json=heartbeat,
            headers={"Authorization": "Bearer provider-status-secret"},
        )
    ).status_code == 200
    view = await client.get("/v1/tools", headers={"Authorization": "Bearer admin-secret"})
    assert view.json()["tools"][0]["reported"][0]["reasons"] == ["descriptor_mismatch"]
    assert view.json()["tools"][0]["effective"]["reasons"] == [
        "inactive_descriptor",
        "descriptor_mismatch",
        "execution_off",
    ]


async def test_concurrent_inventory_replay_creates_one_immutable_snapshot(client, app) -> None:
    await _register_provider(app)
    inventory = _inventory_payload()
    headers = {
        "Authorization": "Bearer provider-status-secret",
        "Idempotency-Key": "status-inventory-concurrent",
    }

    first, second = await asyncio.gather(
        client.post("/v1/provider-inventory/snapshots", json=inventory, headers=headers),
        client.post("/v1/provider-inventory/snapshots", json=inventory, headers=headers),
    )

    assert sorted([first.status_code, second.status_code]) == [200, 201]
    assert first.json()["snapshot_id"] == second.json()["snapshot_id"]
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(ToolProviderInventorySnapshot.id))) == 1
        assert await session.scalar(select(func.count(ToolProviderInventoryEntry.id))) == 1


async def test_database_received_time_drives_inventory_and_heartbeat_staleness(client, app) -> None:
    await _import_descriptor(app)
    await _register_provider(app)
    inventory = _inventory_payload()
    assert (
        await client.post(
            "/v1/provider-inventory/snapshots",
            json=inventory,
            headers={
                "Authorization": "Bearer provider-status-secret",
                "Idempotency-Key": "status-inventory-stale",
            },
        )
    ).status_code == 201
    heartbeat = {
        "schema_version": "1.0",
        "provider_id": "provider-status-primary",
        "inventory_hash": inventory["snapshot_hash"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "health": "healthy",
        "current_concurrency": 0,
        "max_concurrency": 4,
        "metadata": {},
    }
    assert (
        await client.post(
            "/v1/providers/heartbeats",
            json=heartbeat,
            headers={"Authorization": "Bearer provider-status-secret"},
        )
    ).status_code == 200

    old_database_time = datetime.now(timezone.utc) - timedelta(days=2)
    async with app.state.database.sessions() as session:
        snapshot = await session.scalar(select(ToolProviderInventorySnapshot))
        stored_heartbeat = await session.scalar(select(ToolProviderHeartbeat))
        assert snapshot is not None and stored_heartbeat is not None
        snapshot.received_at = old_database_time
        stored_heartbeat.received_at = old_database_time
        await session.commit()

    view = await client.get("/v1/tools", headers={"Authorization": "Bearer admin-secret"})
    assert view.status_code == 200
    assert view.json()["summary"]["fresh_inventories"] == 0
    assert view.json()["summary"]["healthy_providers"] == 0
    assert view.json()["tools"][0]["reported"][0]["reasons"] == [
        "inventory_stale",
        "provider_stale",
    ]
    assert view.json()["tools"][0]["effective"]["reasons"] == [
        "inactive_descriptor",
        "inventory_stale",
        "provider_stale",
        "execution_off",
    ]


def test_provider_registration_json_remains_secret_free() -> None:
    payload = json.dumps(_registration().model_dump(mode="json"), sort_keys=True)
    assert "secret" not in payload


def test_descriptor_admin_source_is_read_from_the_exact_git_commit() -> None:
    repository = Path(__file__).parents[1]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = _git_descriptor_source(
        repository,
        commit,
        Path("packages/contracts/vectors/tool_registry/status.inspect-1.0.0.json"),
    )

    assert source == _descriptor_source()
