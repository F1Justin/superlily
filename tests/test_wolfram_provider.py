from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from typing import Any

import pytest

from superlily_contracts import ToolLeaseOut, canonicalize_json_value, load_tool_descriptor
from superlily_wolfram_provider.main import (
    WolframExecutor,
    WolframProviderConfig,
    _execute_lease,
    _execution_seconds,
    _load_runtime,
)
from superlily_wolfram_provider.runtime import (
    WolframWorkerClient,
    WolframWorkerError,
    build_worker_identity_hash,
    wolfram_implementation_hash,
)


DESCRIPTOR_PATH = Path("registry/descriptors/wolfram.run/1.0.0.json")
WORKER_IDENTITY = "a" * 64


async def _worker_server(
    directory: Path,
    response: dict[str, Any] | bytes,
    *,
    delay_seconds: float = 0.0,
) -> tuple[asyncio.AbstractServer, Path]:
    directory.chmod(0o700)
    socket_path = directory / "worker.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            encoded = (
                response
                if isinstance(response, bytes)
                else json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
            )
            writer.write(encoded + b"\n")
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = await asyncio.start_unix_server(handle, path=socket_path)
    socket_path.chmod(0o600)
    return server, socket_path


def _executor(socket_path: Path) -> WolframExecutor:
    return WolframExecutor(
        DESCRIPTOR_PATH.read_bytes(),
        worker_identity_hash=WORKER_IDENTITY,
        worker_socket=socket_path,
        connect_timeout_seconds=1,
    )


def _config(socket_path: Path) -> WolframProviderConfig:
    return WolframProviderConfig(
        core_url="http://core.test",
        token="provider-token",
        worker_identity_hash=WORKER_IDENTITY,
        descriptor_path=DESCRIPTOR_PATH,
        worker_socket=socket_path,
        heartbeat_seconds=5,
        inventory_seconds=5,
        http_timeout_seconds=1,
        connect_timeout_seconds=1,
        poll_seconds=0.05,
        max_idle_poll_seconds=0.1,
        execution_heartbeat_seconds=0.1,
    )


def test_wolfram_descriptor_is_text_only_and_explicit_about_sandboxed_subprocesses() -> None:
    loaded = load_tool_descriptor(DESCRIPTOR_PATH.read_bytes())
    descriptor = loaded.descriptor
    assert descriptor.tool_id == "wolfram.run"
    assert descriptor.version == "1.0.0"
    assert descriptor.retry_policy == "no_automatic_retry"
    assert descriptor.natural_language is False
    assert descriptor.execution_permissions.filesystem == "sandbox_only"
    assert descriptor.execution_permissions.subprocess == "sandbox_only"
    assert descriptor.execution_permissions.artifacts == []
    assert descriptor.resource_budget.artifact_bytes is None
    assert descriptor.output_schema["properties"]["kind"]["const"] == "text"


def test_implementation_hash_binds_worker_identity_and_provider_sources() -> None:
    worker_identity = build_worker_identity_hash(
        image_id="sha256:" + "0" * 64,
        server_sha256="1" * 64,
        kernel_wrapper_sha256="2" * 64,
        engine_version="15.0.0",
        sandbox_profile_sha256="3" * 64,
    )
    assert len(worker_identity) == 64
    assert worker_identity == build_worker_identity_hash(
        image_id="sha256:" + "0" * 64,
        server_sha256="1" * 64,
        kernel_wrapper_sha256="2" * 64,
        engine_version="15.0.0",
        sandbox_profile_sha256="3" * 64,
    )
    first = wolfram_implementation_hash("1" * 64)
    assert len(first) == 64
    assert first == wolfram_implementation_hash("1" * 64)
    assert first != wolfram_implementation_hash("2" * 64)
    with pytest.raises(ValueError, match="worker_identity_hash"):
        wolfram_implementation_hash("not-a-hash")
    with pytest.raises(ValueError, match="image ID"):
        build_worker_identity_hash(
            image_id="latest",
            server_sha256="1" * 64,
            kernel_wrapper_sha256="2" * 64,
            engine_version="15.0.0",
            sandbox_profile_sha256="3" * 64,
        )
    with pytest.raises(ValueError, match="sandbox"):
        build_worker_identity_hash(
            image_id="sha256:" + "0" * 64,
            server_sha256="1" * 64,
            kernel_wrapper_sha256="2" * 64,
            engine_version="15.0.0",
            sandbox_profile_sha256="not-a-hash",
        )


def test_reporter_inventory_is_ineligible_while_executor_inventory_reports_hard_bounds(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "worker.sock"
    report_executor, reported = _load_runtime(_config(socket_path), execution_enabled=False)
    run_executor, executable = _load_runtime(_config(socket_path), execution_enabled=True)
    assert report_executor.implementation_hash == run_executor.implementation_hash
    assert set(reported.inventory_entry.budget_enforcement.values()) == {"unsupported"}
    assert executable.inventory_entry.budget_enforcement == {
        "input_bytes": "hard",
        "memory": "hard",
        "output_bytes": "hard",
        "wall_time": "hard",
    }


@pytest.mark.asyncio
async def test_worker_health_and_text_result_are_strict_and_bounded(tmp_path: Path) -> None:
    health_server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "status": "ready", "requests": 7, "uid": os.getuid(), "pid": 42},
    )
    try:
        assert await WolframWorkerClient(socket_path).health() == {
            "status": "ready",
            "requests": 7,
            "uid": os.getuid(),
        }
    finally:
        health_server.close()
        await health_server.wait_closed()

    socket_path.unlink(missing_ok=True)
    text_server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "kind": "text", "text": "4"},
    )
    try:
        result = await _executor(socket_path).execute(
            {"expression": "2+2"},
            timeout_seconds=2,
        )
        assert result.outcome == "success"
        assert result.output == {"kind": "text", "text": "4"}
        assert result.usage.input_bytes == len(
            canonicalize_json_value({"expression": "2+2"}).canonical_bytes
        )
        assert result.usage.output_bytes == len(
            canonicalize_json_value({"kind": "text", "text": "4"}).canonical_bytes
        )
    finally:
        text_server.close()
        await text_server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_code"),
    [
        ({"ok": True, "kind": "image", "data": "AAAA"}, "invalid_output"),
        ({"ok": True, "kind": "audio", "data": "AAAA"}, "invalid_output"),
        ({"ok": False, "error": "raw backend detail"}, "execution_failed"),
        ({"ok": True, "kind": "text", "text": "4", "unknown": True}, "invalid_output"),
        ({"ok": True, "kind": "text", "text": "4", "rotating": "yes"}, "invalid_output"),
    ],
)
async def test_worker_non_text_error_and_schema_drift_fail_closed(
    tmp_path: Path,
    response: dict[str, Any],
    error_code: str,
) -> None:
    server, socket_path = await _worker_server(tmp_path, response)
    try:
        result = await _executor(socket_path).execute(
            {"expression": "Plot[x,{x,0,1}]"},
            timeout_seconds=2,
        )
        assert result.outcome == "failure"
        assert result.error_code == error_code
        assert "raw backend detail" not in (result.safe_detail or "")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [b"{", b"x" * (32 * 1024 + 1)])
async def test_worker_malformed_or_oversized_transport_fails_closed(
    tmp_path: Path,
    response: bytes,
) -> None:
    server, socket_path = await _worker_server(tmp_path, response)
    try:
        result = await _executor(socket_path).execute(
            {"expression": "2+2"},
            timeout_seconds=2,
        )
        assert result.outcome == "failure"
        assert result.error_code == "invalid_output"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_expression_byte_limit_is_stricter_than_character_schema(
    tmp_path: Path,
) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "kind": "text", "text": "unexpected"},
    )
    try:
        result = await _executor(socket_path).execute(
            {"expression": "界" * 3_000},
            timeout_seconds=2,
        )
        assert result.outcome == "failure"
        assert result.error_code == "invalid_output"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_socket_authority_rejects_wide_permissions(tmp_path: Path) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "status": "ready", "requests": 0, "uid": os.getuid(), "pid": 1},
    )
    socket_path.chmod(0o666)
    try:
        with pytest.raises(WolframWorkerError, match="authority"):
            await WolframWorkerClient(socket_path).health()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_timeout_is_bounded_without_automatic_retry(tmp_path: Path) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "kind": "text", "text": "late"},
        delay_seconds=1.2,
    )
    try:
        result = await _executor(socket_path).execute(
            {"expression": "Pause[10]"},
            timeout_seconds=1,
        )
        assert result.outcome == "failure"
        assert result.error_code == "timeout"
    finally:
        await asyncio.sleep(1.25)
        server.close()
        await server.wait_closed()


class _ExecutionClient:
    def __init__(self, *, cancel_requested: bool = False) -> None:
        self.cancel_requested = cancel_requested
        self.starts: list[Any] = []
        self.heartbeats: list[Any] = []
        self.completions: list[Any] = []
        self.failures: list[Any] = []

    async def start(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.starts.append((invocation_id, payload))
        return {"state": "running"}

    async def heartbeat(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.heartbeats.append((invocation_id, payload))
        return {"cancel_requested": self.cancel_requested}

    async def complete(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.completions.append((invocation_id, payload))
        return {"state": "succeeded"}

    async def fail(self, invocation_id: str, payload: Any) -> dict[str, Any]:
        self.failures.append((invocation_id, payload))
        return {"state": "failed"}


def _lease(implementation: Any, *, lifetime_seconds: float = 3) -> ToolLeaseOut:
    descriptor = implementation.loaded_descriptor.descriptor
    now = datetime.now(timezone.utc)
    payload = {"expression": "2+2"}
    return ToolLeaseOut(
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        attempt_number=1,
        fencing_token=1,
        lease_secret="s" * 32,
        provider_id="provider-wolfram-primary",
        inventory_hash="b" * 64,
        implementation_hash=implementation.inventory_entry.implementation_hash,
        tool_id=descriptor.tool_id,
        descriptor_version=descriptor.version,
        descriptor_hash=implementation.inventory_entry.descriptor_hash,
        input=payload,
        input_hash=canonicalize_json_value(payload).sha256,
        deadline_at=now + timedelta(seconds=lifetime_seconds),
        lease_expires_at=now + timedelta(seconds=1),
        resource_budget=descriptor.resource_budget,
        execution_permissions=descriptor.execution_permissions,
    )


@pytest.mark.asyncio
async def test_execution_heartbeats_past_initial_lease_and_completes(tmp_path: Path) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "kind": "text", "text": "4"},
        delay_seconds=0.25,
    )
    client = _ExecutionClient()
    wolfram, implementation = _load_runtime(_config(socket_path), execution_enabled=True)
    lease = _lease(implementation)
    try:
        assert _execution_seconds(lease, descriptor_timeout_ms=60_000) > 1
        await _execute_lease(
            client,  # type: ignore[arg-type]
            wolfram,
            implementation,
            lease,
            _config(socket_path),
            inventory_hash="b" * 64,
        )
        assert len(client.starts) == 1
        assert len(client.heartbeats) >= 2
        assert len(client.completions) == 1
        assert client.failures == []
        assert client.completions[0][1].output == {"kind": "text", "text": "4"}
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_unconfirmable_worker_cancellation_does_not_emit_false_cancel_ack(
    tmp_path: Path,
) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": True, "kind": "text", "text": "late"},
        delay_seconds=0.25,
    )
    client = _ExecutionClient(cancel_requested=True)
    wolfram, implementation = _load_runtime(_config(socket_path), execution_enabled=True)
    try:
        await _execute_lease(
            client,  # type: ignore[arg-type]
            wolfram,
            implementation,
            _lease(implementation),
            _config(socket_path),
            inventory_hash="b" * 64,
        )
        assert len(client.starts) == 1
        assert len(client.heartbeats) == 1
        assert client.completions == []
        assert client.failures == []
    finally:
        await asyncio.sleep(0.3)
        server.close()
        await server.wait_closed()
