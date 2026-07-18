from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from superlily_contracts import (
    ProviderRegistration,
    ToolExecutionStartIn,
    ToolLeaseOut,
    canonicalize_json_value,
    load_tool_descriptor,
)
from superlily_core.tool_registry_service import import_tool_descriptor, register_tool_provider
from superlily_provider_sdk import (
    ProviderRegistryClient,
    ProviderExecutionClient,
    ProviderExecutionError,
    ProviderReportError,
    ProviderToolImplementation,
)
from superlily_status_provider.status import status_implementation_hash


ROOT = Path(__file__).parents[1]
AUTHORITY_PATH = ROOT / "registry/descriptors/status.inspect/1.0.0.json"
VECTOR_PATH = ROOT / "packages/contracts/vectors/tool_registry/status.inspect-1.0.0.json"
REGISTRATION_PATH = ROOT / "registry/providers/provider-status-primary.json"


def _implementation(source: bytes | None = None) -> ProviderToolImplementation:
    return ProviderToolImplementation.from_descriptor(
        source or AUTHORITY_PATH.read_bytes(),
        implementation_hash=status_implementation_hash(),
        budget_enforcement={"output_bytes": "hard", "wall_time": "unsupported"},
    )


def test_sdk_uses_the_shared_descriptor_parser_and_golden_hash() -> None:
    source = VECTOR_PATH.read_bytes()
    implementation = _implementation(source)
    loaded = load_tool_descriptor(source)

    assert implementation.loaded_descriptor.authority.canonical_bytes == loaded.authority.canonical_bytes
    assert implementation.inventory_entry.descriptor_hash == loaded.authority.sha256
    assert implementation.loaded_descriptor.descriptor.description.endswith(
        "snapshot.  Semantic spacing is preserved."
    )


async def test_sdk_publishes_exact_authority_and_honest_runtime_capabilities(app, client) -> None:
    source = AUTHORITY_PATH.read_bytes()
    descriptor_hash = load_tool_descriptor(source).authority.sha256
    registration = ProviderRegistration.model_validate_json(REGISTRATION_PATH.read_bytes())
    async with app.state.database.sessions() as session:
        await import_tool_descriptor(
            session,
            source,
            source_commit="3" * 40,
            bundle_hash=descriptor_hash,
            reviewer="phase3-reviewer",
        )
    async with app.state.database.sessions() as session:
        await register_tool_provider(
            session,
            registration,
            actor="phase3-reviewer",
            settings=app.state.settings,
        )

    reporter = ProviderRegistryClient(
        base_url="http://test",
        provider_id="provider-status-primary",
        token="provider-status-secret",
        tools=[_implementation()],
        max_concurrency=4,
        client=client,
    )
    observed_at = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    inventory = reporter.build_inventory(observed_at=observed_at)
    first = await reporter.publish_inventory(inventory)
    replay = await reporter.publish_inventory(inventory)
    heartbeat = reporter.build_heartbeat(
        inventory_hash=inventory.snapshot_hash,
        health="healthy",
        metadata={"execution_enabled": False, "role": "registry_reporter"},
        observed_at=observed_at,
    )
    heartbeat_receipt = await reporter.publish_heartbeat(heartbeat)

    assert first["duplicate"] is False
    assert replay == {**first, "duplicate": True}
    assert heartbeat_receipt["inventory_hash"] == inventory.snapshot_hash
    view = await client.get("/v1/tools", headers={"Authorization": "Bearer admin-secret"})
    assert view.status_code == 200
    body = view.json()
    tool = body["tools"][0]
    assert tool["desired"]["descriptor_hash"] == descriptor_hash
    assert tool["reported"][0] == {
        "provider_id": "provider-status-primary",
        "inventory_hash": inventory.snapshot_hash,
        "implementation_hash": status_implementation_hash(),
        "budget_enforcement": {
            "output_bytes": "hard",
            "wall_time": "unsupported",
        },
        "heartbeat_health": "healthy",
        "reasons": ["budget_unenforceable"],
        "runtime_eligible": False,
    }
    assert tool["effective"] == {
        "eligible": False,
        "execution_mode": "off",
        "reasons": ["inactive_descriptor", "budget_unenforceable", "execution_off"],
    }
    assert body["execution"] == {
        "mode": "off",
        "global_stop": False,
        "invocation_endpoints": False,
        "lease_endpoint": True,
        "leases_enabled": False,
        "natural_language_callers": False,
        "active_rollout_plan": None,
    }
    assert (await client.post("/v1/tool-invocations", json={})).status_code == 401


async def test_sdk_retries_same_inventory_request_without_leaking_secret() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"detail": "temporary"})
        return httpx.Response(
            201,
            json={
                "provider_id": "provider-status-primary",
                "snapshot_id": "snapshot-1",
                "snapshot_hash": "1" * 64,
                "duplicate": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        reporter = ProviderRegistryClient(
            base_url="https://core.example.test",
            provider_id="provider-status-primary",
            token="provider-token-that-must-not-leak",
            tools=[_implementation()],
            max_concurrency=4,
            report_attempts=2,
            retry_backoff_seconds=0,
            client=http_client,
        )
        inventory = reporter.build_inventory(
            observed_at=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        )
        await reporter.publish_inventory(inventory)

    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert requests[0].headers["Authorization"] == "Bearer provider-token-that-must-not-leak"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"token": "echo"}))
    ) as http_client:
        reporter = ProviderRegistryClient(
            base_url="https://core.example.test",
            provider_id="provider-status-primary",
            token="provider-token-that-must-not-leak",
            tools=[_implementation()],
            max_concurrency=4,
            client=http_client,
        )
        with pytest.raises(ProviderReportError) as failure:
            await reporter.publish_inventory(reporter.build_inventory())
    assert "provider-token-that-must-not-leak" not in str(failure.value)
    assert "echo" not in str(failure.value)


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/core",
        "https://user:secret@core.example.test",
        "https://core.example.test?token=secret",
        " https://core.example.test",
    ],
)
def test_sdk_rejects_unsafe_core_urls(base_url: str) -> None:
    with pytest.raises(ValueError):
        ProviderRegistryClient(
            base_url=base_url,
            provider_id="provider-status-primary",
            token="provider-token",
            tools=[_implementation()],
            max_concurrency=4,
        )


async def test_execution_sdk_is_single_shot_and_validates_the_lease() -> None:
    requests: list[httpx.Request] = []

    def unavailable(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"detail": "temporary"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http_client:
        executor = ProviderExecutionClient(
            base_url="https://core.example.test",
            provider_id="provider-status-primary",
            token="execution-token-that-must-not-leak",
            client=http_client,
        )
        with pytest.raises(ProviderExecutionError) as failure:
            await executor.request_lease("a" * 64)
    assert len(requests) == 1
    assert requests[0].headers["connection"] == "close"
    assert "execution-token-that-must-not-leak" not in str(failure.value)

    input_value = {"scope": "provider_runtime"}
    lease = ToolLeaseOut(
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        attempt_number=1,
        fencing_token=1,
        lease_secret="s" * 43,
        provider_id="provider-status-primary",
        inventory_hash="a" * 64,
        implementation_hash="b" * 64,
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash="c" * 64,
        input=input_value,
        input_hash=canonicalize_json_value(input_value).sha256,
        deadline_at=datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
        lease_expires_at=datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
        resource_budget={"output_bytes": 32768},
        execution_permissions={
            "network": "deny",
            "filesystem": "deny",
            "subprocess": "deny",
            "secrets": [],
            "remote_fetch": "deny",
            "artifacts": [],
        },
    )
    lease_requests: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(204),
            httpx.Response(200, json=lease.model_dump(mode="json")),
        ]
    )

    def lease_response(request: httpx.Request) -> httpx.Response:
        lease_requests.append(request)
        return next(responses)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lease_response)
    ) as http_client:
        executor = ProviderExecutionClient(
            base_url="https://core.example.test",
            provider_id="provider-status-primary",
            token="provider-token",
            client=http_client,
        )
        assert await executor.request_lease("a" * 64) is None
        received = await executor.request_lease("a" * 64)
    assert received == lease
    assert [request.headers["connection"] for request in lease_requests] == [
        "close",
        "close",
    ]


async def test_execution_sdk_rejects_empty_non_lease_receipt() -> None:
    requests: list[httpx.Request] = []

    def empty_response(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(empty_response)
    ) as http_client:
        executor = ProviderExecutionClient(
            base_url="https://core.example.test",
            provider_id="provider-status-primary",
            token="provider-token",
            client=http_client,
        )
        with pytest.raises(ProviderExecutionError, match="empty execution receipt"):
            await executor.start(
                "invocation-1",
                ToolExecutionStartIn.model_validate(
                    {
                        "attempt_id": "attempt-1",
                        "fencing_token": 1,
                        "lease_secret": "s" * 43,
                    }
                ),
            )
    assert requests[0].headers["connection"] != "close"
