from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from superlily_contracts import ToolRegistryContractError, load_tool_descriptor, validate_schema_instance
from superlily_status_provider.main import _load_runtime
from superlily_status_provider.status import StatusInspector, status_implementation_hash


AUTHORITY_PATH = (
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
