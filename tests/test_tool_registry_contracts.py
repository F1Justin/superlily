from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from superlily_contracts import (
    CanonicalJSONError,
    ProviderHeartbeatIn,
    ProviderInventorySnapshotIn,
    ProviderInventoryTool,
    SchemaProfileError,
    ToolDescriptor,
    ToolRegistryContractError,
    canonicalize_json,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
    strict_json_loads,
    validate_schema_instance,
    validate_schema_profile,
)


VECTOR_ROOT = Path(__file__).parents[1] / "packages/contracts/vectors/tool_registry"


def _descriptor_source() -> bytes:
    return (VECTOR_ROOT / "status.inspect-1.0.0.json").read_bytes()


def _descriptor_payload() -> dict:
    return json.loads(_descriptor_source())


def test_shared_jcs_golden_vectors() -> None:
    vectors = json.loads((VECTOR_ROOT / "jcs.json").read_text())
    for vector in vectors:
        result = canonicalize_json(vector["source"].encode())
        assert result.canonical_bytes.decode() == vector["canonical"], vector["name"]
        assert result.sha256 == vector["sha256"], vector["name"]


def test_descriptor_golden_hash_and_semantic_whitespace() -> None:
    loaded = load_tool_descriptor(_descriptor_source())
    expected_hash = (VECTOR_ROOT / "status.inspect-1.0.0.sha256").read_text().strip()

    assert loaded.authority.sha256 == expected_hash
    assert loaded.descriptor.tool_id == "status.inspect"
    assert loaded.descriptor.description.endswith("snapshot.  Semantic spacing is preserved.")
    assert b"snapshot.  Semantic spacing is preserved." in loaded.authority.canonical_bytes


def test_cli_uses_the_same_descriptor_hash_vector() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "superlily_contracts.tool_registry_cli",
            "verify-descriptor",
            str(VECTOR_ROOT / "status.inspect-1.0.0.json"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result == {
        "canonical_bytes": 1674,
        "descriptor_hash": (VECTOR_ROOT / "status.inspect-1.0.0.sha256").read_text().strip(),
        "tool_id": "status.inspect",
        "version": "1.0.0",
    }


def test_descriptor_hash_ignores_object_key_order_only() -> None:
    payload = _descriptor_payload()
    reordered = dict(reversed(list(payload.items())))

    original = load_tool_descriptor(_descriptor_source())
    reordered_result = load_tool_descriptor(json.dumps(reordered, ensure_ascii=False).encode())

    assert reordered_result.authority.sha256 == original.authority.sha256
    assert reordered_result.authority.canonical_bytes == original.authority.canonical_bytes


def test_shared_rejection_vectors() -> None:
    vectors = json.loads((VECTOR_ROOT / "rejections.json").read_text())
    for vector in vectors:
        source = vector["source"].encode()
        if vector["layer"] == "json":
            with pytest.raises(CanonicalJSONError):
                strict_json_loads(source)
        else:
            schema = strict_json_loads(source)
            with pytest.raises(SchemaProfileError):
                validate_schema_profile(schema)


@pytest.mark.parametrize(
    "source",
    [
        b"\xef\xbb\xbf{}",
        b'{"a":1,"a":2}',
        b'{"value":Infinity}',
        b'{"value":1e400}',
        b"",
    ],
)
def test_strict_json_rejects_ambiguous_or_noncanonical_input(source: bytes) -> None:
    with pytest.raises(CanonicalJSONError):
        canonicalize_json(source)


def test_descriptor_rejects_extra_authority_and_agent_callers() -> None:
    payload = _descriptor_payload()
    payload["unexpected"] = True
    with pytest.raises(ToolRegistryContractError):
        load_tool_descriptor(json.dumps(payload).encode())

    payload = _descriptor_payload()
    payload["allowed_callers"] = ["agent"]
    payload["natural_language"] = True
    with pytest.raises(ToolRegistryContractError):
        load_tool_descriptor(json.dumps(payload).encode())


def test_state_changing_descriptor_requires_confirmation_and_no_blind_retry() -> None:
    payload = _descriptor_payload()
    payload["side_effect"] = "write"
    payload["confirmation"] = "never"
    with pytest.raises(ValidationError):
        ToolDescriptor.model_validate(payload)


def test_artifact_descriptor_requires_complete_hard_bounded_policy() -> None:
    payload = _descriptor_payload()
    payload["execution_permissions"]["artifacts"] = ["image/png"]
    with pytest.raises(ValidationError, match="policy"):
        ToolDescriptor.model_validate(payload)

    payload["artifact_policy"] = {
        "max_count": 1,
        "max_single_bytes": 1_048_576,
        "max_width_pixels": 2048,
        "max_height_pixels": 2048,
        "reservation_ttl_seconds": 120,
    }
    payload["resource_budget"]["artifact_bytes"] = 1_048_576
    with pytest.raises(ValidationError, match="hard enforcement"):
        ToolDescriptor.model_validate(payload)

    payload["required_budget_enforcement"].append("artifact_bytes")
    descriptor = ToolDescriptor.model_validate(payload)
    assert descriptor.artifact_policy is not None
    assert descriptor.artifact_policy.max_count == 1

    payload["artifact_policy"]["max_single_bytes"] = 2_097_152
    with pytest.raises(ValidationError, match="single artifact"):
        ToolDescriptor.model_validate(payload)

    payload["confirmation"] = "on_write"
    payload["retry_policy"] = "retry_safe"
    with pytest.raises(ValidationError):
        ToolDescriptor.model_validate(payload)


def test_restricted_schema_validates_instances_without_remote_resolution() -> None:
    schema = _descriptor_payload()["input_schema"]

    validate_schema_instance({"scope": "core"}, schema)
    with pytest.raises(ToolRegistryContractError):
        validate_schema_instance({"scope": ""}, schema)
    with pytest.raises(ToolRegistryContractError):
        validate_schema_instance({"scope": "core", "extra": True}, schema)
    with pytest.raises(ToolRegistryContractError):
        validate_schema_instance({"scope": float("nan")}, schema)


def test_schema_profile_requires_an_explicit_dialect() -> None:
    schema = {
        "type": "string",
        "minLength": 0,
        "maxLength": 16,
    }

    with pytest.raises(SchemaProfileError):
        validate_schema_profile(schema)


def test_local_definition_is_allowed_but_cycles_are_not() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "Status": {
                "type": "string",
                "minLength": 2,
                "maxLength": 16,
                "enum": ["ok", "degraded"],
            }
        },
        "type": "object",
        "properties": {"status": {"$ref": "#/$defs/Status"}},
        "required": ["status"],
        "additionalProperties": False,
        "minProperties": 1,
        "maxProperties": 1,
    }

    validate_schema_profile(schema)
    validate_schema_instance({"status": "ok"}, schema)


def _inventory_tool(tool_id: str, descriptor_hash: str, implementation_hash: str) -> ProviderInventoryTool:
    return ProviderInventoryTool(
        tool_id=tool_id,
        descriptor_version="1.0.0",
        descriptor_hash=descriptor_hash,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash=implementation_hash,
        budget_enforcement={"output_bytes": "hard", "wall_time": "hard"},
    )


def test_provider_inventory_hash_is_order_independent_and_verified() -> None:
    first = _inventory_tool("status.inspect", "1" * 64, "a" * 64)
    second = _inventory_tool("wolfram.run", "2" * 64, "b" * 64)
    expected = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[first, second],
    )
    reversed_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[second, first],
    )

    assert expected == reversed_hash
    with pytest.raises(ToolRegistryContractError):
        provider_inventory_snapshot_hash(
            provider_id="provider-status-primary",
            protocol_version="unknown-provider-protocol",
            tools=[first],
        )

    snapshot = ProviderInventorySnapshotIn(
        provider_id="provider-status-primary",
        snapshot_hash=expected,
        observed_at=datetime.now(timezone.utc),
        protocol_version="superlily-provider-pull-v1",
        tools=[second, first],
    )
    assert snapshot.snapshot_hash == expected

    with pytest.raises(ValidationError):
        ProviderInventorySnapshotIn(
            provider_id="provider-status-primary",
            snapshot_hash="0" * 64,
            observed_at=datetime.now(timezone.utc),
            protocol_version="superlily-provider-pull-v1",
            tools=[first, second],
        )


def test_provider_inventory_rejects_two_versions_of_the_same_tool() -> None:
    first = _inventory_tool("status.inspect", "1" * 64, "a" * 64)
    second = first.model_copy(
        update={
            "descriptor_version": "2.0.0",
            "descriptor_hash": "2" * 64,
            "implementation_hash": "b" * 64,
        }
    )
    snapshot_hash = provider_inventory_snapshot_hash(
        provider_id="provider-status-primary",
        protocol_version="superlily-provider-pull-v1",
        tools=[first, second],
    )

    with pytest.raises(ValidationError, match="tool IDs must be unique"):
        ProviderInventorySnapshotIn(
            provider_id="provider-status-primary",
            snapshot_hash=snapshot_hash,
            observed_at=datetime.now(timezone.utc),
            protocol_version="superlily-provider-pull-v1",
            tools=[first, second],
        )


def test_provider_heartbeat_is_bound_to_inventory_and_capacity() -> None:
    heartbeat = ProviderHeartbeatIn(
        provider_id="provider-status-primary",
        inventory_hash="1" * 64,
        observed_at=datetime.now(timezone.utc),
        health="healthy",
        current_concurrency=1,
        max_concurrency=4,
        metadata={"worker_version": "1.0.0"},
    )
    assert heartbeat.inventory_hash == "1" * 64

    with pytest.raises(ValidationError):
        ProviderHeartbeatIn(
            provider_id="provider-status-primary",
            inventory_hash="1" * 64,
            observed_at=datetime.now(timezone.utc),
            health="healthy",
            current_concurrency=5,
            max_concurrency=4,
        )
