import asyncio
import importlib.util
import logging
import sys
import time
import types
from pathlib import Path

import httpx
import pytest


def load_reporter_module(
    path: Path = Path("bridges/lily_nonebot/lily_core_bridge/reporter.py"),
):
    if "nekro" in path.parts and "nekro_agent.api.core" not in sys.modules:
        nekro_package = types.ModuleType("nekro_agent")
        api_package = types.ModuleType("nekro_agent.api")
        core_module = types.ModuleType("nekro_agent.api.core")
        core_module.logger = logging.getLogger("nekro_bridge_reporter_test")
        sys.modules["nekro_agent"] = nekro_package
        sys.modules["nekro_agent.api"] = api_package
        sys.modules["nekro_agent.api.core"] = core_module
    module_name = f"bridge_reporter_test_{path.parts[-3]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_full_bridge_queue_drops_without_blocking() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter("http://127.0.0.1:9", "token", 1, 0.05)
    item = module.ReportItem("/v1/events", {"example": True}, "stable-key")

    started = time.perf_counter()
    accepted = reporter.enqueue(item)
    dropped = reporter.enqueue(item)
    elapsed = time.perf_counter() - started

    assert reporter.queue.qsize() == 1
    assert reporter.dropped == 1
    assert accepted is True
    assert dropped is False
    assert elapsed < 0.05


def test_bridge_with_empty_token_is_silent_noop() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter("http://127.0.0.1:8765", "", 1, 0.05)
    assert reporter.enqueue(module.ReportItem("/v1/events", {"example": True})) is False
    assert reporter.queue.empty()
    assert reporter.dropped == 0


def test_claim_and_background_report_timeouts_are_independent() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter(
        "http://127.0.0.1:8765",
        "token",
        1,
        claim_timeout_seconds=1.0,
        report_timeout_seconds=2.0,
    )

    assert reporter.claim_timeout_seconds == 1.0
    assert reporter.report_timeout_seconds == 2.0


@pytest.mark.parametrize(
    "path",
    [
        Path("bridges/lily_nonebot/lily_core_bridge/reporter.py"),
        Path("bridges/nekro/superlily_bridge/reporter.py"),
    ],
)
@pytest.mark.asyncio
async def test_claim_and_ack_retry_transient_failures_with_stable_keys(path: Path) -> None:
    module = load_reporter_module(path)
    reporter = module.BackgroundReporter(
        "http://127.0.0.1:8765",
        "instance-token",
        10,
        claim_timeout_seconds=10.0,
        report_timeout_seconds=10.0,
        claim_attempts=2,
        claim_retry_backoff_seconds=0,
    )
    calls: list[tuple[str, str | None]] = []
    endpoint_attempts: dict[str, int] = {"evaluate": 0, "ack": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        endpoint = "evaluate" if request.url.path.endswith("/evaluate") else "ack"
        endpoint_attempts[endpoint] += 1
        calls.append((endpoint, request.headers.get("Idempotency-Key")))
        if endpoint_attempts[endpoint] == 1:
            raise httpx.ReadTimeout("transient checkpoint stall", request=request)
        return httpx.Response(
            200,
            request=request,
            json={"claim_id": "claim-123", "action": "deny"},
        )

    reporter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    claim = await reporter.request_claim({"event": True}, "event-key-123")
    acknowledged = await reporter.acknowledge_claim("claim-123")
    await reporter._client.aclose()

    assert claim == {"claim_id": "claim-123", "action": "deny"}
    assert acknowledged is True
    assert calls == [
        ("evaluate", "event-key-123"),
        ("evaluate", "event-key-123"),
        ("ack", "claim-ack-claim-123"),
        ("ack", "claim-ack-claim-123"),
    ]
    assert reporter.claim_failures == 0
    assert reporter.claim_ack_failures == 0


@pytest.mark.parametrize(
    "path",
    [
        Path("bridges/lily_nonebot/lily_core_bridge/reporter.py"),
        Path("bridges/nekro/superlily_bridge/reporter.py"),
    ],
)
@pytest.mark.asyncio
async def test_background_reports_retry_transient_failures_with_same_idempotency_key(
    path: Path,
) -> None:
    module = load_reporter_module(path)
    reporter = module.BackgroundReporter(
        "http://127.0.0.1:8765",
        "instance-token",
        10,
        claim_timeout_seconds=3.0,
        report_timeout_seconds=5.0,
        report_attempts=3,
        report_retry_backoff_seconds=0,
    )
    attempts = 0
    idempotency_keys: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        idempotency_keys.append(request.headers.get("Idempotency-Key"))
        if attempts < 3:
            raise httpx.ReadTimeout("transient database delay", request=request)
        return httpx.Response(201, request=request)

    reporter._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter._worker = asyncio.create_task(reporter._run())
    assert reporter.enqueue(module.ReportItem("/v1/events", {"event": True}, "event-key"))
    await asyncio.wait_for(reporter.queue.join(), timeout=1)
    await reporter.stop()

    assert attempts == 3
    assert idempotency_keys == ["event-key", "event-key", "event-key"]
    assert reporter.dropped == 0


@pytest.mark.asyncio
async def test_claim_and_suppression_ack_use_synchronous_control_plane_requests() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter(
        "http://127.0.0.1:8765",
        "instance-token",
        1,
        claim_timeout_seconds=1.0,
        report_timeout_seconds=2.0,
    )

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/evaluate"):
                return Response({"claim_id": "claim-123", "action": "deny"})
            return Response({"claim_id": "claim-123", "acknowledged_at": "now"})

    client = Client()
    reporter._client = client

    claim = await reporter.request_claim({"event": True}, "event-key-123")
    acknowledged = await reporter.acknowledge_claim("claim-123")

    assert claim == {"claim_id": "claim-123", "action": "deny"}
    assert acknowledged is True
    assert [call[0] for call in client.calls] == [
        "http://127.0.0.1:8765/v1/claims/evaluate",
        "http://127.0.0.1:8765/v1/claims/claim-123/ack",
    ]
    assert client.calls[0][1]["headers"]["Idempotency-Key"] == "event-key-123"
    assert client.calls[1][1]["headers"]["Idempotency-Key"] == "claim-ack-claim-123"
