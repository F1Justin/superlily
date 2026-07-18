"""Provider Registry reporting SDK.

This Phase 3a SDK reports exact descriptor/implementation identity and health.
It deliberately has no invocation, lease, execution, artifact, or platform-send
surface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from superlily_contracts import (
    PROVIDER_PROTOCOL_V1,
    LoadedToolDescriptor,
    ProviderHeartbeatIn,
    ProviderInventorySnapshotIn,
    ProviderInventoryTool,
    canonicalize_json_value,
    load_tool_descriptor,
    provider_inventory_snapshot_hash,
)


class ProviderReportError(RuntimeError):
    """A bounded provider control-plane report failed."""


@dataclass(frozen=True, slots=True)
class ProviderToolImplementation:
    """One runtime implementation bound to exact reviewed descriptor bytes."""

    loaded_descriptor: LoadedToolDescriptor
    inventory_entry: ProviderInventoryTool

    @classmethod
    def from_descriptor(
        cls,
        descriptor_source: bytes,
        *,
        implementation_hash: str,
        budget_enforcement: Mapping[str, str],
    ) -> "ProviderToolImplementation":
        loaded = load_tool_descriptor(descriptor_source)
        entry = ProviderInventoryTool.model_validate(
            {
                "tool_id": loaded.descriptor.tool_id,
                "descriptor_version": loaded.descriptor.version,
                "descriptor_hash": loaded.authority.sha256,
                "protocol_version": loaded.descriptor.provider_selector.protocol,
                "implementation_hash": implementation_hash,
                "budget_enforcement": dict(budget_enforcement),
            }
        )
        return cls(loaded_descriptor=loaded, inventory_entry=entry)


class ProviderRegistryClient:
    """Authenticated inventory/heartbeat client with bounded safe retries."""

    def __init__(
        self,
        *,
        base_url: str,
        provider_id: str,
        token: str,
        tools: Sequence[ProviderToolImplementation],
        max_concurrency: int,
        timeout_seconds: float = 5.0,
        report_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = self._validated_base_url(base_url)
        if not token or token != token.strip():
            raise ValueError("provider token must be an exact non-empty string")
        if not tools:
            raise ValueError("provider must report at least one tool implementation")
        if not 1 <= max_concurrency <= 10_000:
            raise ValueError("max_concurrency must be between 1 and 10000")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= report_attempts <= 10:
            raise ValueError("report_attempts must be between 1 and 10")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")

        ordered_tools = tuple(sorted(tools, key=lambda item: item.inventory_entry.tool_id))
        if len({item.inventory_entry.tool_id for item in ordered_tools}) != len(ordered_tools):
            raise ValueError("provider tool IDs must be unique")
        for item in ordered_tools:
            selector = item.loaded_descriptor.descriptor.provider_selector
            if provider_id not in selector.provider_ids:
                raise ValueError("provider is not selected by one of its descriptors")
            if selector.protocol != PROVIDER_PROTOCOL_V1:
                raise ValueError("provider descriptor protocol is unsupported")

        self.provider_id = provider_id
        self._token = token
        self.tools = ordered_tools
        self.max_concurrency = max_concurrency
        self.timeout_seconds = timeout_seconds
        self.report_attempts = report_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self._client = client
        self._owns_client = client is None

    @staticmethod
    def _validated_base_url(value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("Core base URL must be an exact non-empty URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
            raise ValueError("Core base URL must use http or https and include a host")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Core base URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Core base URL must not contain a query or fragment")
        return value.rstrip("/")

    async def __aenter__(self) -> "ProviderRegistryClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def build_inventory(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> ProviderInventorySnapshotIn:
        entries = [item.inventory_entry for item in self.tools]
        snapshot_hash = provider_inventory_snapshot_hash(
            provider_id=self.provider_id,
            protocol_version=PROVIDER_PROTOCOL_V1,
            tools=entries,
        )
        return ProviderInventorySnapshotIn(
            provider_id=self.provider_id,
            snapshot_hash=snapshot_hash,
            observed_at=observed_at or datetime.now(timezone.utc),
            protocol_version=PROVIDER_PROTOCOL_V1,
            tools=entries,
        )

    @staticmethod
    def inventory_idempotency_key(payload: ProviderInventorySnapshotIn) -> str:
        authority = canonicalize_json_value(payload.model_dump(mode="json"))
        return f"provider-inventory-{authority.sha256}"

    def build_heartbeat(
        self,
        *,
        inventory_hash: str,
        health: str,
        current_concurrency: int = 0,
        oldest_work_age_ms: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        observed_at: datetime | None = None,
    ) -> ProviderHeartbeatIn:
        return ProviderHeartbeatIn.model_validate(
            {
                "provider_id": self.provider_id,
                "inventory_hash": inventory_hash,
                "observed_at": observed_at or datetime.now(timezone.utc),
                "health": health,
                "current_concurrency": current_concurrency,
                "max_concurrency": self.max_concurrency,
                "oldest_work_age_ms": oldest_work_age_ms,
                "metadata": dict(metadata or {}),
            }
        )

    async def publish_inventory(
        self,
        payload: ProviderInventorySnapshotIn,
    ) -> dict[str, Any]:
        return await self._post(
            "/v1/provider-inventory/snapshots",
            payload.model_dump(mode="json"),
            idempotency_key=self.inventory_idempotency_key(payload),
        )

    async def publish_heartbeat(
        self,
        payload: ProviderHeartbeatIn,
    ) -> dict[str, Any]:
        return await self._post(
            "/v1/providers/heartbeats",
            payload.model_dump(mode="json"),
        )

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        client = await self._ensure_client()
        headers = {"Authorization": f"Bearer {self._token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        last_error: Exception | None = None
        for attempt in range(self.report_attempts):
            try:
                response = await client.post(
                    f"{self.base_url}{endpoint}",
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise ProviderReportError("Core returned a non-object provider receipt")
                return result
            except ProviderReportError:
                raise
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code != 429 and exc.response.status_code < 500:
                    raise ProviderReportError(
                        f"Core rejected provider report with HTTP {exc.response.status_code}"
                    ) from exc
            except (httpx.TransportError, ValueError) as exc:
                last_error = exc
            if attempt + 1 < self.report_attempts:
                await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))

        assert last_error is not None
        if isinstance(last_error, httpx.HTTPStatusError):
            detail = f"HTTP {last_error.response.status_code}"
        else:
            detail = type(last_error).__name__
        raise ProviderReportError(
            f"provider report failed after {self.report_attempts} attempts ({detail})"
        ) from last_error
