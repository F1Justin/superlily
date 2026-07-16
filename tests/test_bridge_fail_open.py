import importlib.util
import sys
import time
from pathlib import Path

import pytest


def load_reporter_module():
    path = Path("bridges/lily_nonebot/lily_core_bridge/reporter.py")
    spec = importlib.util.spec_from_file_location("lily_bridge_reporter_test", path)
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
