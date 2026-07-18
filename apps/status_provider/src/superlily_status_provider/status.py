"""``status.inspect`` 的有界本地实现；本模块没有平台发送能力。"""

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
    """绑定本地工具、硬超时监督器与 Provider 协议编排的确切源码。"""

    digest = sha256()
    for name in ("executor.py", "main.py", "status.py"):
        source = Path(__file__).with_name(name).read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name.encode("utf-8"))
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    return digest.hexdigest()


class StatusInspector:
    """Small structured liveness operation with no external I/O or side effect."""

    def __init__(
        self,
        loaded_descriptor: LoadedToolDescriptor,
        *,
        implementation_hash: str | None = None,
    ) -> None:
        descriptor = loaded_descriptor.descriptor
        if descriptor.tool_id != "status.inspect" or descriptor.version not in {
            "1.0.0",
            "1.0.1",
        }:
            raise ValueError("StatusInspector requires a reviewed status.inspect 1.0.x descriptor")
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
