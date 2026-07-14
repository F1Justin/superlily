import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nekro_agent.api.core import logger


@dataclass(slots=True)
class ReportItem:
    endpoint: str
    payload: dict[str, Any]
    idempotency_key: str | None = None


class BackgroundReporter:
    def __init__(
        self,
        base_url: str,
        token: str,
        queue_size: int,
        claim_timeout_seconds: float,
        report_timeout_seconds: float | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.queue: asyncio.Queue[ReportItem] = asyncio.Queue(maxsize=queue_size)
        self.claim_timeout_seconds = claim_timeout_seconds
        self.report_timeout_seconds = report_timeout_seconds or claim_timeout_seconds
        self.dropped = 0
        self.claim_failures = 0
        self._client: httpx.AsyncClient | None = None
        self._worker: asyncio.Task | None = None
        self._last_warning = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def start(self) -> None:
        if not self.enabled or self._worker:
            return
        self._client = httpx.AsyncClient(timeout=self.report_timeout_seconds, trust_env=False)
        self._worker = asyncio.create_task(self._run(), name="nekro-lily-core-reporter")

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self._client:
            await self._client.aclose()
            self._client = None

    def enqueue(self, item: ReportItem) -> bool:
        if not self.enabled:
            return False
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self._warn_limited("Lily Core queue full; telemetry dropped")
            return False

    def _warn_limited(self, message: str, total: int | None = None) -> None:
        now = time.monotonic()
        if now - self._last_warning >= 60:
            logger.warning(f"{message} (total={self.dropped if total is None else total})")
            self._last_warning = now

    async def request_claim(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        if not self.enabled or self._client is None:
            return None
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/claims/evaluate",
                json=payload,
                timeout=self.claim_timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Idempotency-Key": idempotency_key,
                },
            )
            response.raise_for_status()
            body = response.json()
            return body if isinstance(body, dict) else None
        except Exception as exc:
            self.claim_failures += 1
            self._warn_limited(
                f"Lily Core claim failed open: {type(exc).__name__}",
                self.claim_failures,
            )
            return None

    async def _run(self) -> None:
        assert self._client is not None
        while True:
            item = await self.queue.get()
            headers = {"Authorization": f"Bearer {self.token}"}
            if item.idempotency_key:
                headers["Idempotency-Key"] = item.idempotency_key
            try:
                response = await self._client.post(
                    f"{self.base_url}{item.endpoint}",
                    json=item.payload,
                    headers=headers,
                )
                response.raise_for_status()
            except Exception as exc:
                self.dropped += 1
                self._warn_limited(f"Lily Core report failed: {type(exc).__name__}")
            finally:
                self.queue.task_done()
