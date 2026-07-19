"""Provider 拉取执行客户端；所有变更请求都只发送一次。"""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Any

import httpx

from superlily_contracts import (
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolArtifactFinalizeIn,
    ToolArtifactReference,
    ToolArtifactReservationOut,
    ToolArtifactReserveIn,
    ToolArtifactUploadOut,
    ToolLeaseOut,
    ToolLeaseRequestIn,
)

from .client import ProviderRegistryClient


class ProviderExecutionError(RuntimeError):
    """不包含 credential 或 Core 原始 body 的有界执行协议错误。"""


class ProviderExecutionClient:
    """
    lease/start/heartbeat/complete/fail 线协议客户端。

    这些请求不自动重试：响应丢失会导致结果不明，应由 lease
    过期、fence 和 Core 账本恢复，不能由 SDK 盲目重放。
    """

    def __init__(
        self,
        *,
        base_url: str,
        provider_id: str,
        token: str,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = ProviderRegistryClient._validated_base_url(base_url)
        if not provider_id or provider_id != provider_id.strip():
            raise ValueError("provider_id must be exact non-empty text")
        if not token or token != token.strip():
            raise ValueError("provider token must be exact non-empty text")
        if timeout_seconds <= 0:
            raise ValueError("execution timeout must be positive")
        self.provider_id = provider_id
        self._token = token
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "ProviderExecutionClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def request_lease(self, inventory_hash: str) -> ToolLeaseOut | None:
        payload = ToolLeaseRequestIn(inventory_hash=inventory_hash)
        response = await self._post(
            "/v1/tool-executions/lease",
            payload.model_dump(mode="json"),
            close_connection=True,
        )
        if response is None:
            return None
        try:
            return ToolLeaseOut.model_validate(response)
        except ValueError as exc:
            raise ProviderExecutionError("Core returned an invalid bounded lease") from exc

    async def start(self, invocation_id: str, payload: ToolExecutionStartIn) -> dict[str, Any]:
        result = await self._post(
            f"/v1/tool-executions/{invocation_id}/start",
            payload.model_dump(mode="json"),
        )
        return self._require_receipt(result)

    async def heartbeat(
        self,
        invocation_id: str,
        payload: ToolExecutionHeartbeatIn,
    ) -> dict[str, Any]:
        result = await self._post(
            f"/v1/tool-executions/{invocation_id}/heartbeat",
            payload.model_dump(mode="json"),
        )
        return self._require_receipt(result)

    async def reserve_artifact(
        self,
        invocation_id: str,
        payload: ToolArtifactReserveIn,
        *,
        idempotency_key: str,
    ) -> ToolArtifactReservationOut:
        if not 8 <= len(idempotency_key) <= 256 or idempotency_key != idempotency_key.strip():
            raise ValueError("artifact idempotency key must be exact bounded text")
        result = self._require_receipt(
            await self._post(
                f"/v1/tool-executions/{invocation_id}/artifacts/reserve",
                payload.model_dump(mode="json"),
                idempotency_key=idempotency_key,
            )
        )
        bounded = dict(result)
        bounded.pop("duplicate", None)
        try:
            return ToolArtifactReservationOut.model_validate(bounded)
        except ValueError as exc:
            raise ProviderExecutionError(
                "Core returned an invalid artifact reservation"
            ) from exc

    async def upload_artifact(
        self,
        artifact_id: str,
        *,
        upload_secret: str,
        mime_type: str,
        content: bytes | AsyncIterable[bytes],
    ) -> ToolArtifactUploadOut:
        if not upload_secret or upload_secret != upload_secret.strip():
            raise ValueError("artifact upload secret must be exact non-empty text")
        if not mime_type or mime_type != mime_type.strip():
            raise ValueError("artifact MIME must be exact non-empty text")
        client = await self._ensure_client()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-Superlily-Artifact-Upload-Secret": upload_secret,
            "Content-Type": mime_type,
        }
        if isinstance(content, bytes):
            headers["Content-Length"] = str(len(content))
        try:
            response = await client.put(
                f"{self.base_url}/v1/tool-artifacts/{artifact_id}/content",
                content=content,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ProviderExecutionError(
                    "Core returned a non-object artifact upload receipt"
                )
            try:
                return ToolArtifactUploadOut.model_validate(result)
            except ValueError as exc:
                raise ProviderExecutionError(
                    "Core returned an invalid artifact upload receipt"
                ) from exc
        except ProviderExecutionError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderExecutionError(
                f"Core rejected artifact upload with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.TransportError, ValueError) as exc:
            raise ProviderExecutionError(
                f"artifact upload had an ambiguous {type(exc).__name__} failure"
            ) from exc

    async def finalize_artifact(
        self,
        invocation_id: str,
        payload: ToolArtifactFinalizeIn,
    ) -> ToolArtifactReference:
        result = self._require_receipt(
            await self._post(
                f"/v1/tool-executions/{invocation_id}/artifacts/finalize",
                payload.model_dump(mode="json"),
            )
        )
        try:
            return ToolArtifactReference.model_validate(result)
        except ValueError as exc:
            raise ProviderExecutionError(
                "Core returned an invalid finalized artifact reference"
            ) from exc

    async def complete(
        self,
        invocation_id: str,
        payload: ToolExecutionCompleteIn,
    ) -> dict[str, Any]:
        result = await self._post(
            f"/v1/tool-executions/{invocation_id}/complete",
            payload.model_dump(mode="json"),
        )
        return self._require_receipt(result)

    async def fail(
        self,
        invocation_id: str,
        payload: ToolExecutionFailIn,
    ) -> dict[str, Any]:
        result = await self._post(
            f"/v1/tool-executions/{invocation_id}/fail",
            payload.model_dump(mode="json"),
        )
        return self._require_receipt(result)

    @staticmethod
    def _require_receipt(result: dict[str, Any] | None) -> dict[str, Any]:
        if result is None:
            raise ProviderExecutionError("Core returned an empty execution receipt")
        return result

    async def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        *,
        close_connection: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        client = await self._ensure_client()
        headers = {"Authorization": f"Bearer {self._token}"}
        # 空 lease 会退避到 5 秒，恰好可能撞上常见 ASGI 服务的 5 秒
        # keep-alive 回收边界。只关闭轮询连接，避免滚动重启或边界竞态
        # 产生 ReadError；真实执行阶段的 start/heartbeat/complete 仍复用连接。
        if close_connection:
            headers["Connection"] = "close"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = await client.post(
                f"{self.base_url}{endpoint}",
                json=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            if response.status_code == 204:
                return None
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ProviderExecutionError("Core returned a non-object execution receipt")
            return result
        except ProviderExecutionError:
            raise
        except httpx.HTTPStatusError as exc:
            raise ProviderExecutionError(
                f"Core rejected execution operation with HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.TransportError, ValueError) as exc:
            raise ProviderExecutionError(
                f"execution operation had an ambiguous {type(exc).__name__} failure"
            ) from exc
