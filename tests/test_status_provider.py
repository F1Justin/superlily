from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import multiprocessing
from pathlib import Path
from typing import Any

import pytest

from superlily_contracts import (
    ToolLeaseOut,
    ToolRegistryContractError,
    canonicalize_json_value,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
    validate_schema_instance,
)
from superlily_status_provider.executor import StatusProcessSupervisor
from superlily_status_provider.main import (
    StatusProviderConfig,
    _execute_lease,
    _load_runtime,
    _next_idle_poll_seconds,
)
from superlily_status_provider.status import StatusInspector, status_implementation_hash


AUTHORITY_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.1.json"
)
LEGACY_AUTHORITY_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/status.inspect/1.0.0.json"
)


def test_real_status_authority_is_bound_to_the_standalone_implementation() -> None:
    source = AUTHORITY_PATH.read_bytes()
    inspector, implementation = _load_runtime(AUTHORITY_PATH)

    assert inspector.loaded_descriptor.authority.sha256 == load_tool_descriptor(source).authority.sha256
    assert inspector.loaded_descriptor.descriptor.source_plugin == "superlily_status_provider.status"
    assert implementation.inventory_entry.implementation_hash == status_implementation_hash()
    assert implementation.inventory_entry.budget_enforcement == {
        "output_bytes": "hard",
        "wall_time": "unsupported",
    }
    _, executable = _load_runtime(AUTHORITY_PATH, execution_enabled=True)
    assert executable.inventory_entry.budget_enforcement == {
        "output_bytes": "hard",
        "wall_time": "hard",
    }


def test_status_provider_idle_poll_backoff_is_bounded_and_resets_after_work() -> None:
    config = StatusProviderConfig(
        core_url="http://core.test",
        token="provider-token",
        poll_seconds=0.25,
        max_idle_poll_seconds=5,
    )

    delay = config.poll_seconds
    for expected in (0.5, 1, 2, 4, 5, 5):
        delay = _next_idle_poll_seconds(config, delay, lease_received=False)
        assert delay == expected
    assert _next_idle_poll_seconds(config, delay, lease_received=True) == 0.25

    with pytest.raises(ValueError, match="max idle poll interval"):
        StatusProviderConfig(
            core_url="http://core.test",
            token="provider-token",
            poll_seconds=1,
            max_idle_poll_seconds=0.5,
        )


def test_status_1_0_1_changes_only_version_and_measured_memory_budget() -> None:
    legacy = load_tool_descriptor(LEGACY_AUTHORITY_PATH.read_bytes()).descriptor.model_dump(
        mode="json"
    )
    current = load_tool_descriptor(AUTHORITY_PATH.read_bytes()).descriptor.model_dump(mode="json")

    assert current["version"] == "1.0.1"
    assert current["resource_budget"]["memory_bytes"] == 268_435_456
    current["version"] = legacy["version"]
    current["resource_budget"]["memory_bytes"] = legacy["resource_budget"]["memory_bytes"]
    assert current == legacy


def test_status_inspector_returns_only_bounded_structured_data() -> None:
    loaded = load_tool_descriptor(AUTHORITY_PATH.read_bytes())
    inspector = StatusInspector(loaded, implementation_hash="a" * 64)
    checked_at = datetime(2026, 7, 18, 12, 34, 56, tzinfo=timezone.utc)

    result = inspector.inspect({"scope": "provider_runtime"}, checked_at=checked_at)

    assert result == {
        "status": "ok",
        "checked_at": "2026-07-18T12:34:56Z",
        "scope": "provider_runtime",
        "provider_id": "provider-status-primary",
        "descriptor_hash": loaded.authority.sha256,
        "implementation_hash": "a" * 64,
    }
    validate_schema_instance(result, loaded.descriptor.output_schema)


def test_status_inspector_rejects_scope_expansion_and_naive_time() -> None:
    loaded = load_tool_descriptor(AUTHORITY_PATH.read_bytes())
    inspector = StatusInspector(loaded)

    with pytest.raises(ToolRegistryContractError):
        inspector.inspect({"scope": "host"})
    with pytest.raises(ValueError, match="timezone"):
        inspector.inspect(
            {"scope": "provider_runtime"},
            checked_at=datetime(2026, 7, 18, 12, 34, 56),
        )
    with pytest.raises(ValueError, match="SHA-256"):
        StatusInspector(loaded, implementation_hash="not-a-hash")


async def test_status_process_supervisor_returns_bounded_usage_and_output() -> None:
    descriptor_source = AUTHORITY_PATH.read_bytes()
    implementation_hash = status_implementation_hash()
    supervisor = StatusProcessSupervisor(
        descriptor_source,
        implementation_hash=implementation_hash,
    )

    result = await supervisor.execute(
        {"scope": "provider_runtime"},
        timeout_seconds=3,
    )

    assert result.outcome == "success"
    assert result.output is not None
    assert result.output["implementation_hash"] == implementation_hash
    assert result.usage.input_bytes == len(
        canonicalize_json_value({"scope": "provider_runtime"}).canonical_bytes
    )
    assert result.usage.output_bytes == len(
        canonicalize_json_value(result.output).canonical_bytes
    )
    assert result.usage.wall_time_ms > 0
    assert result.usage.memory_peak_bytes > 0


async def test_status_process_supervisor_hard_kills_an_overdue_child() -> None:
    supervisor = StatusProcessSupervisor(
        AUTHORITY_PATH.read_bytes(),
        implementation_hash=status_implementation_hash(),
    )

    result = await supervisor.execute(
        {"scope": "provider_runtime"},
        timeout_seconds=0.001,
    )

    assert result.outcome == "failure"
    assert result.error_code == "timeout"
    assert "hard wall time" in (result.safe_detail or "")
    assert not any(
        child.name == "superlily-status-inspect" and child.is_alive()
        for child in multiprocessing.active_children()
    )


async def test_status_process_supervisor_cancellation_reaps_its_child() -> None:
    supervisor = StatusProcessSupervisor(
        AUTHORITY_PATH.read_bytes(),
        implementation_hash=status_implementation_hash(),
    )
    execution = asyncio.create_task(
        supervisor.execute({"scope": "provider_runtime"}, timeout_seconds=3)
    )
    await asyncio.sleep(0.001)
    execution.cancel()
    with suppress(asyncio.CancelledError):
        await execution

    assert execution.cancelled()
    assert not any(
        child.name == "superlily-status-inspect" and child.is_alive()
        for child in multiprocessing.active_children()
    )


class _RecordingExecutionClient:
    def __init__(self) -> None:
        self.operations: list[tuple[str, Any]] = []

    async def start(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.operations.append(("start", payload))
        return {"state": "running"}

    async def heartbeat(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.operations.append(("heartbeat", payload))
        return {"state": "running", "cancel_requested": False}

    async def complete(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.operations.append(("complete", payload))
        return {"state": "succeeded"}

    async def fail(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.operations.append(("fail", payload))
        return {"state": "failed"}


async def test_exact_status_lease_runs_start_then_complete_without_platform_send() -> None:
    descriptor_source = AUTHORITY_PATH.read_bytes()
    _, implementation = _load_runtime(AUTHORITY_PATH, execution_enabled=True)
    inventory_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[implementation.inventory_entry],
    )
    input_value = {"scope": "provider_runtime"}
    deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
    lease = ToolLeaseOut(
        invocation_id="invocation-status-1",
        attempt_id="attempt-status-1",
        attempt_number=1,
        fencing_token=1,
        lease_secret="s" * 43,
        provider_id="provider-status-primary",
        inventory_hash=inventory_hash,
        implementation_hash=implementation.inventory_entry.implementation_hash,
        tool_id="status.inspect",
        descriptor_version=implementation.loaded_descriptor.descriptor.version,
        descriptor_hash=implementation.inventory_entry.descriptor_hash,
        input=input_value,
        input_hash=canonicalize_json_value(input_value).sha256,
        deadline_at=deadline,
        lease_expires_at=deadline,
        resource_budget=implementation.loaded_descriptor.descriptor.resource_budget,
        execution_permissions=implementation.loaded_descriptor.descriptor.execution_permissions,
    )
    client = _RecordingExecutionClient()
    supervisor = StatusProcessSupervisor(
        descriptor_source,
        implementation_hash=implementation.inventory_entry.implementation_hash,
    )
    config = StatusProviderConfig(
        core_url="http://core.test",
        token="provider-token",
        descriptor_path=AUTHORITY_PATH,
        execution_heartbeat_seconds=0.1,
    )

    await _execute_lease(  # type: ignore[arg-type]
        client,
        supervisor,
        implementation,
        lease,
        config,
        inventory_hash=inventory_hash,
    )

    assert [name for name, _ in client.operations][0] == "start"
    assert [name for name, _ in client.operations][-1] == "complete"
    assert not any(name == "fail" for name, _ in client.operations)
    completion = client.operations[-1][1]
    assert completion.output["status"] == "ok"
    assert completion.output["scope"] == "provider_runtime"
