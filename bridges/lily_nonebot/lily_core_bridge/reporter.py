import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("lily_core_bridge")


@dataclass(slots=True)
class ReportItem:
    endpoint: str
    payload: dict[str, Any]
    idempotency_key: str | None = None


class BackgroundReporter:
    """Bounded fail-open HTTP reporter.

    `enqueue` never performs I/O. A full queue drops telemetry rather than
    applying backpressure to the bot.
    """

    def __init__(self, base_url: str, token: str, queue_size: int, timeout_seconds: float):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.queue: asyncio.Queue[ReportItem] = asyncio.Queue(maxsize=queue_size)
        self.timeout_seconds = timeout_seconds
        self.dropped = 0
        self._client: httpx.AsyncClient | None = None
        self._worker: asyncio.Task | None = None
        self._last_warning = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def start(self) -> None:
        if not self.enabled or self._worker:
            return
        self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        self._worker = asyncio.create_task(self._run(), name="lily-core-reporter")

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

    def enqueue(self, item: ReportItem) -> None:
        if not self.enabled:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            self._warn_limited("Lily Core queue full; telemetry dropped")

    def _warn_limited(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_warning >= 60:
            logger.warning("%s (total dropped=%d)", message, self.dropped)
            self._last_warning = now

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
            except (httpx.HTTPError, ValueError) as exc:
                self.dropped += 1
                self._warn_limited(f"Lily Core report failed: {type(exc).__name__}")
            finally:
                self.queue.task_done()

