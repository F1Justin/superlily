import asyncio
from datetime import datetime, timezone
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .spool import DurableIngressSpool, ReceiptMismatch, SpoolError, SpoolRecord

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
        report_attempts: int = 3,
        report_retry_backoff_seconds: float = 0.1,
        claim_attempts: int = 2,
        claim_retry_backoff_seconds: float = 0.1,
        spool_path: str = "",
        spool_quota_bytes: int = 268_435_456,
        spool_retention_seconds: int = 86_400,
        spool_max_record_bytes: int = 1_048_576,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.queue: asyncio.Queue[ReportItem] = asyncio.Queue(maxsize=queue_size)
        self.claim_timeout_seconds = claim_timeout_seconds
        self.report_timeout_seconds = report_timeout_seconds or claim_timeout_seconds
        self.report_attempts = max(1, report_attempts)
        self.report_retry_backoff_seconds = max(0.0, report_retry_backoff_seconds)
        self.claim_attempts = max(1, claim_attempts)
        self.claim_retry_backoff_seconds = max(0.0, claim_retry_backoff_seconds)
        self.spool_path = spool_path.strip()
        self.dropped = 0
        self.spool_capture_failures = 0
        self.claim_failures = 0
        self.claim_ack_failures = 0
        self._client: httpx.AsyncClient | None = None
        self._worker: asyncio.Task | None = None
        self._spool_worker: asyncio.Task | None = None
        self._worker_restart_handle: asyncio.TimerHandle | None = None
        self._spool_worker_restart_handle: asyncio.TimerHandle | None = None
        self._stopping = False
        self.worker_restarts = 0
        self.spool_worker_restarts = 0
        self.last_worker_error: str | None = None
        self._spool_wakeup = asyncio.Event()
        self._spool: DurableIngressSpool | None = (
            DurableIngressSpool(
                self.spool_path,
                quota_bytes=spool_quota_bytes,
                retention_seconds=spool_retention_seconds,
                max_record_bytes=spool_max_record_bytes,
            )
            if self.spool_path
            else None
        )
        self._spool_start_error: str | None = None
        self._last_warning = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.token)

    async def start(self) -> None:
        if not self.enabled:
            return
        self._stopping = False
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.report_timeout_seconds,
                trust_env=False,
            )
            if self._spool is not None:
                try:
                    self._spool.open()
                except Exception as exc:
                    self._spool_start_error = f"{type(exc).__name__}: {exc}"[:4096]
                    logger.error(
                        f"Lily Core durable spool failed to open: {self._spool_start_error}"
                    )
                else:
                    self._spool_start_error = None
                    self._spool_wakeup.set()
        self._start_workers()

    async def stop(self) -> None:
        self._stopping = True
        for handle in (self._worker_restart_handle, self._spool_worker_restart_handle):
            if handle:
                handle.cancel()
        self._worker_restart_handle = None
        self._spool_worker_restart_handle = None
        for task in (self._worker, self._spool_worker):
            if task:
                task.cancel()
        for task in (self._worker, self._spool_worker):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._worker = None
        self._spool_worker = None
        if self._client:
            await self._client.aclose()
            self._client = None
        if self._spool is not None:
            self._spool.close()

    def worker_status(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "reporter": self._worker_state(
                self._worker,
                self._worker_restart_handle,
                available=self.enabled,
            ),
            "durable_spool": self._worker_state(
                self._spool_worker,
                self._spool_worker_restart_handle,
                available=self._spool is not None and self._spool_start_error is None,
            ),
            "reporter_restarts": self.worker_restarts,
            "durable_spool_restarts": self.spool_worker_restarts,
            "last_error": self.last_worker_error,
        }

    def _worker_state(
        self,
        task: asyncio.Task | None,
        restart_handle: asyncio.TimerHandle | None,
        *,
        available: bool,
    ) -> str:
        if not available:
            return "disabled"
        if self._stopping:
            return "stopping"
        if task is not None and not task.done():
            return "running"
        if restart_handle is not None:
            return "restarting"
        return "stopped"

    def _start_workers(self) -> None:
        if self._stopping or self._client is None:
            return
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._run(),
                name="nekro-lily-core-reporter",
            )
            self._worker.add_done_callback(self._reporter_done)
        if (
            self._spool is not None
            and self._spool_start_error is None
            and self._spool_worker is None
        ):
            self._spool_worker = asyncio.create_task(
                self._run_spool(),
                name="nekro-lily-core-durable-spool",
            )
            self._spool_worker.add_done_callback(self._spool_reporter_done)

    def _reporter_done(self, task: asyncio.Task) -> None:
        if task is not self._worker:
            return
        self._worker = None
        if self._stopping:
            return
        self._record_worker_exit("reporter", task)
        self._worker_restart_handle = asyncio.get_running_loop().call_later(
            1.0,
            self._restart_reporter,
        )

    def _spool_reporter_done(self, task: asyncio.Task) -> None:
        if task is not self._spool_worker:
            return
        self._spool_worker = None
        if self._stopping:
            return
        self._record_worker_exit("durable_spool", task)
        self._spool_worker_restart_handle = asyncio.get_running_loop().call_later(
            1.0,
            self._restart_spool_reporter,
        )

    def _record_worker_exit(self, worker: str, task: asyncio.Task) -> None:
        if task.cancelled():
            reason = "CancelledError"
        else:
            exception = task.exception()
            reason = type(exception).__name__ if exception is not None else "UnexpectedReturn"
        if worker == "reporter":
            self.worker_restarts += 1
        else:
            self.spool_worker_restarts += 1
        self.last_worker_error = f"{worker}:{reason}"
        logger.error(
            "Lily Core background worker exited unexpectedly; "
            f"worker={worker} reason={reason}"
        )

    def _restart_reporter(self) -> None:
        self._worker_restart_handle = None
        self._start_workers()

    def _restart_spool_reporter(self) -> None:
        self._spool_worker_restart_handle = None
        self._start_workers()

    def enqueue(self, item: ReportItem) -> bool:
        if not self.enabled:
            return False
        if item.endpoint == "/v1/events" and self._spool is not None:
            return self._capture_durable_event(item) is not None
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            self._warn_limited("Lily Core queue full; telemetry dropped")
            return False

    def _capture_durable_event(self, item: ReportItem) -> SpoolRecord | None:
        if self._spool is None or not item.idempotency_key:
            self.spool_capture_failures += 1
            self.dropped += 1
            self._warn_limited("Lily Core durable event has no spool or idempotency key")
            return None
        if self._spool_start_error is not None:
            self.spool_capture_failures += 1
            self.dropped += 1
            self._warn_limited("Lily Core durable spool is unavailable")
            return None
        try:
            record = self._spool.append_event(item.payload, item.idempotency_key)
        except SpoolError as exc:
            # Expected spool rejections already increment their persistent counters.
            self.dropped += 1
            self._warn_limited(
                f"Lily Core durable capture rejected: {type(exc).__name__}"
            )
            return None
        except Exception as exc:
            self.spool_capture_failures += 1
            self.dropped += 1
            self._warn_limited(
                f"Lily Core durable capture failed: {type(exc).__name__}",
                self.spool_capture_failures,
            )
            return None
        self._spool_wakeup.set()
        return record

    def spool_status(self) -> dict[str, Any] | None:
        if self._spool is None:
            return None
        if self._spool_start_error is not None:
            return {
                "schema_version": "1.0",
                "enabled": True,
                "state": "error",
                "durability_mode": "sqlite_full",
                "spool_id": None,
                "pending_records": 0,
                "pending_bytes": 0,
                "committed_records": 0,
                "quarantined_records": 0,
                "quarantined_files": 0,
                "oldest_pending_seconds": None,
                "live_bytes": 0,
                "quota_bytes": self._spool.quota_bytes,
                "highest_sequence": 0,
                "replay_successes": 0,
                "replay_failures": 0,
                "capture_failures": self.spool_capture_failures,
                "quota_rejections": 0,
                "last_error": self._spool_start_error,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
        try:
            status = self._spool.status()
        except Exception as exc:
            self._spool_start_error = f"{type(exc).__name__}: {exc}"[:4096]
            return self.spool_status()
        status["capture_failures"] += self.spool_capture_failures
        return status

    def _warn_limited(self, message: str, total: int | None = None) -> None:
        now = time.monotonic()
        if now - self._last_warning >= 60:
            logger.warning(f"{message} (total={self.dropped if total is None else total})")
            self._last_warning = now

    async def request_claim(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        if not self.enabled or self._client is None:
            return None
        durable_record = None
        if self._spool is not None:
            durable_record = self._capture_durable_event(
                ReportItem("/v1/events", payload, idempotency_key)
            )
            if durable_record is None:
                return None
        failure: Exception | None = None
        for attempt in range(self.claim_attempts):
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
                if not isinstance(body, dict):
                    return None
                if durable_record is not None:
                    receipt = body.get("ingest_receipt")
                    if isinstance(receipt, dict):
                        try:
                            self._spool.acknowledge(durable_record, receipt)
                        except ReceiptMismatch as exc:
                            self._spool.retry(durable_record, f"claim_receipt:{exc}")
                return body
            except Exception as exc:
                failure = exc
                if not self._retryable(exc) or attempt + 1 >= self.claim_attempts:
                    break
                await asyncio.sleep(self.claim_retry_backoff_seconds * (2**attempt))
        assert failure is not None
        self.claim_failures += 1
        self._warn_limited(
            f"Lily Core claim failed open: {type(failure).__name__}",
            self.claim_failures,
        )
        return None

    async def acknowledge_claim(self, claim_id: str) -> bool:
        if not self.enabled or self._client is None or not claim_id:
            return False
        failure: Exception | None = None
        for attempt in range(self.claim_attempts):
            try:
                response = await self._client.post(
                    f"{self.base_url}/v1/claims/{claim_id}/ack",
                    timeout=self.claim_timeout_seconds,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Idempotency-Key": f"claim-ack-{claim_id}",
                    },
                )
                response.raise_for_status()
                return True
            except Exception as exc:
                failure = exc
                if not self._retryable(exc) or attempt + 1 >= self.claim_attempts:
                    break
                await asyncio.sleep(self.claim_retry_backoff_seconds * (2**attempt))
        assert failure is not None
        self.claim_ack_failures += 1
        self._warn_limited(
            f"Lily Core claim ack failed safely: {type(failure).__name__}",
            self.claim_ack_failures,
        )
        return False

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        return isinstance(exc, httpx.TransportError) or (
            isinstance(exc, httpx.HTTPStatusError)
            and (exc.response.status_code == 429 or exc.response.status_code >= 500)
        )

    async def _run(self) -> None:
        assert self._client is not None
        while True:
            item = await self.queue.get()
            headers = {"Authorization": f"Bearer {self.token}"}
            if item.idempotency_key:
                headers["Idempotency-Key"] = item.idempotency_key
            try:
                failure: Exception | None = None
                for attempt in range(self.report_attempts):
                    try:
                        response = await self._client.post(
                            f"{self.base_url}{item.endpoint}",
                            json=item.payload,
                            headers=headers,
                        )
                        response.raise_for_status()
                        failure = None
                        break
                    except Exception as exc:
                        failure = exc
                        retryable = self._retryable(exc)
                        if not retryable or attempt + 1 >= self.report_attempts:
                            break
                        await asyncio.sleep(self.report_retry_backoff_seconds * (2**attempt))
                if failure is not None:
                    self.dropped += 1
                    self._warn_limited(f"Lily Core report failed: {type(failure).__name__}")
            finally:
                self.queue.task_done()

    async def _run_spool(self) -> None:
        assert self._client is not None
        assert self._spool is not None
        while True:
            try:
                record = self._spool.next_pending()
            except Exception as exc:
                # A transient SQLite error must not silently kill the replay task.
                logger.exception(
                    "Lily Core durable spool could not read its next record: %s", exc
                )
                await asyncio.sleep(5)
                continue
            if record is None:
                self._spool_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._spool_wakeup.wait(),
                        timeout=self._spool.next_retry_delay(),
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                response = await self._client.post(
                    f"{self.base_url}{record.endpoint}",
                    json=record.payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Idempotency-Key": record.idempotency_key,
                    },
                )
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict):
                    raise ReceiptMismatch("Core event response is not an object")
                self._spool.acknowledge(record, body)
            except Exception as exc:
                if isinstance(exc, httpx.HTTPStatusError) and not self._retryable(exc):
                    self._spool.quarantine(
                        record.sequence,
                        f"http_{exc.response.status_code}:{exc.response.text[:1024]}",
                    )
                else:
                    self._spool.retry(record, f"{type(exc).__name__}: {exc}")
