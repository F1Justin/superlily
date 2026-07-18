"""Bounded local implementation of ``status.inspect``.

The Phase 3a process only invokes this as a local self-test. Core cannot create
an invocation or lease yet, and this module has no platform delivery surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from superlily_contracts import (
    LoadedToolDescriptor,
    canonicalize_json_value,
    validate_schema_instance,
)


PROVIDER_ID = "provider-status-primary"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def status_implementation_hash() -> str:
    """Identify the exact installed source of the bounded status operation."""

    return sha256(Path(__file__).read_bytes()).hexdigest()


class StatusInspector:
    """Small structured liveness operation with no external I/O or side effect."""

    def __init__(
        self,
        loaded_descriptor: LoadedToolDescriptor,
        *,
        implementation_hash: str | None = None,
    ) -> None:
        descriptor = loaded_descriptor.descriptor
        if descriptor.tool_id != "status.inspect" or descriptor.version != "1.0.0":
            raise ValueError("StatusInspector requires the status.inspect 1.0.0 descriptor")
        if descriptor.source_plugin != "superlily_status_provider.status":
            raise ValueError("status descriptor is not bound to this implementation")
        if PROVIDER_ID not in descriptor.provider_selector.provider_ids:
            raise ValueError("status descriptor does not select this provider")
        self.loaded_descriptor = loaded_descriptor
        self.implementation_hash = implementation_hash or status_implementation_hash()
        if not _SHA256_RE.fullmatch(self.implementation_hash):
            raise ValueError("implementation_hash must be a lowercase SHA-256")

    def inspect(
        self,
        payload: dict[str, Any],
        *,
        checked_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate input and return one schema-validated bounded snapshot."""

        descriptor = self.loaded_descriptor.descriptor
        validate_schema_instance(payload, descriptor.input_schema)
        now = checked_at or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("checked_at must include a timezone")
        result = {
            "status": "ok",
            "checked_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "scope": "provider_runtime",
            "provider_id": PROVIDER_ID,
            "descriptor_hash": self.loaded_descriptor.authority.sha256,
            "implementation_hash": self.implementation_hash,
        }
        validate_schema_instance(result, descriptor.output_schema)
        encoded = canonicalize_json_value(result).canonical_bytes
        limit = descriptor.resource_budget.output_bytes
        if limit is None or len(encoded) > limit:
            raise RuntimeError("status result exceeds its descriptor output budget")
        return result
