import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
import types

import httpx
import pytest


ROOT = Path(__file__).parents[1]
SPOOL_PATHS = [
    ROOT / "bridges/lily_nonebot/lily_core_bridge/spool.py",
    ROOT / "bridges/nekro/superlily_bridge/spool.py",
]
REPORTER_PATHS = [
    ROOT / "bridges/lily_nonebot/lily_core_bridge/reporter.py",
    ROOT / "bridges/nekro/superlily_bridge/reporter.py",
]


def load_module(path: Path, leaf: str):
    package_name = f"durable_test_{path.parts[-3]}_{leaf}"
    if "nekro" in path.parts and "nekro_agent.api.core" not in sys.modules:
        nekro_package = types.ModuleType("nekro_agent")
        api_package = types.ModuleType("nekro_agent.api")
        core_module = types.ModuleType("nekro_agent.api.core")

        class Logger:
            def warning(self, *args, **kwargs):
                pass

            def error(self, *args, **kwargs):
                pass

        core_module.logger = Logger()
        sys.modules["nekro_agent"] = nekro_package
        sys.modules["nekro_agent.api"] = api_package
        sys.modules["nekro_agent.api.core"] = core_module
    package = types.ModuleType(package_name)
    package.__path__ = [str(path.parent)]
    sys.modules[package_name] = package
    module_name = f"{package_name}.{leaf}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def event_payload(index: int = 1) -> dict:
    return {
        "schema_version": "1.0",
        "source_event_id": f"qq:test:{index}",
        "instance": {
            "instance_id": "lily-command",
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": "1",
            "role": "command",
        },
        "event_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "occurred_at": "2026-07-18T00:00:00+00:00",
        "metadata": {},
    }


def receipt(record) -> dict:
    return {
        "receipt_id": f"receipt-{record.sequence}",
        "outcome": "committed",
        "spool_id": record.payload["ingress"]["spool_id"],
        "sequence": record.sequence,
        "record_sha256": record.record_sha256,
    }


def test_bridge_spool_implementations_are_identical() -> None:
    assert SPOOL_PATHS[0].read_bytes() == SPOOL_PATHS[1].read_bytes()


@pytest.mark.parametrize("path", SPOOL_PATHS)
def test_spool_survives_restart_and_rejects_identity_reuse(path: Path, tmp_path: Path) -> None:
    module = load_module(path, "spool")
    database = tmp_path / "ingress.sqlite3"
    spool = module.DurableIngressSpool(str(database))
    spool.open()
    payload = event_payload()
    record = spool.append_event(payload, "event-key-1")
    assert record.sequence == 1
    assert payload["ingress"]["record_sha256"] == record.record_sha256
    # An adapter may replay a backlog at startup and log every delivery at the
    # current time. Keep its reported event time separate from our local,
    # durable capture time instead of rewriting either one.
    assert payload["occurred_at"] == "2026-07-18T00:00:00+00:00"
    assert payload["ingress"]["captured_at"] != payload["occurred_at"]
    assert spool.status()["pending_records"] == 1
    spool.close()

    reopened = module.DurableIngressSpool(str(database))
    reopened.open()
    pending = reopened.next_pending()
    assert pending is not None
    assert pending.sequence == 1
    duplicate_payload = event_payload()
    duplicate = reopened.append_event(duplicate_payload, "event-key-1")
    assert duplicate.sequence == 1
    assert duplicate_payload["ingress"] == pending.payload["ingress"]
    with pytest.raises(module.SpoolConflict):
        reopened.append_event(event_payload(2), "event-key-1")
    reopened.acknowledge(pending, receipt(pending))
    assert reopened.next_pending() is None
    status = reopened.status()
    assert status["pending_records"] == 0
    assert status["committed_records"] == 1
    assert status["replay_successes"] == 1
    assert status["highest_sequence"] == 1
    reopened._require_connection().execute(
        "UPDATE spool_records SET committed_at = '2020-01-01T00:00:00+00:00'"
    )
    reopened.compact(force=True)
    assert reopened.status()["committed_records"] == 0
    late_payload = event_payload()
    late_duplicate = reopened.append_event(late_payload, "event-key-1")
    assert late_duplicate.sequence == 1
    assert late_payload["ingress"] == pending.payload["ingress"]
    assert reopened.next_pending() is None
    reopened.close()


@pytest.mark.parametrize("path", SPOOL_PATHS)
def test_spool_preserves_order_and_quarantines_corrupt_records(
    path: Path,
    tmp_path: Path,
) -> None:
    module = load_module(path, "spool")
    spool = module.DurableIngressSpool(str(tmp_path / "ordered.sqlite3"))
    spool.open()
    first = spool.append_event(event_payload(1), "event-key-1")
    second = spool.append_event(event_payload(2), "event-key-2")
    spool.retry(first, "Core offline", base_seconds=0.1)
    assert spool.next_pending() is None  # Sequence 2 cannot overtake sequence 1.
    time.sleep(0.12)
    assert spool.next_pending().sequence == 1
    spool._require_connection().execute(
        "UPDATE spool_records SET payload_json = ? WHERE sequence = ?",
        (json.dumps({"corrupt": True}), first.sequence),
    )
    assert spool.next_pending().sequence == second.sequence
    status = spool.status()
    assert status["quarantined_records"] == 1
    assert status["state"] == "quarantined"
    spool.close()


@pytest.mark.parametrize("path", SPOOL_PATHS)
def test_spool_recovers_a_corrupt_database_without_deleting_evidence(
    path: Path,
    tmp_path: Path,
) -> None:
    module = load_module(path, "spool")
    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not a sqlite database")
    spool = module.DurableIngressSpool(str(database))
    spool.open()
    status = spool.status()
    assert status["state"] == "quarantined"
    assert status["quarantined_files"] >= 1
    assert list(tmp_path.glob("corrupt.sqlite3.quarantine-*"))
    record = spool.append_event(event_payload(), "recovered-event")
    assert record.sequence == 1
    spool.close()


@pytest.mark.parametrize("path", REPORTER_PATHS)
@pytest.mark.asyncio
async def test_claim_path_spools_before_request_and_accepts_nested_receipt(
    path: Path,
    tmp_path: Path,
) -> None:
    module = load_module(path, "reporter")
    reporter = module.BackgroundReporter(
        "http://core.test",
        "instance-token",
        10,
        1.0,
        spool_path=str(tmp_path / f"claim-{path.parts[-3]}.sqlite3"),
    )
    reporter._spool.open()

    async def claim_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ingress = body["ingress"]
        return httpx.Response(
            200,
            request=request,
            json={
                "claim_id": "claim-1",
                "action": "abstain",
                "ingest_receipt": {
                    "receipt_id": "receipt-claim",
                    "outcome": "committed",
                    "spool_id": ingress["spool_id"],
                    "sequence": ingress["sequence"],
                    "record_sha256": ingress["record_sha256"],
                },
            },
        )

    reporter._client = httpx.AsyncClient(transport=httpx.MockTransport(claim_handler))
    payload = event_payload()
    claim = await reporter.request_claim(payload, "claim-event-key")
    assert claim["claim_id"] == "claim-1"
    assert payload["ingress"]["sequence"] == 1
    status = reporter.spool_status()
    assert status["pending_records"] == 0
    assert status["committed_records"] == 1
    await reporter.stop()


@pytest.mark.parametrize("path", REPORTER_PATHS)
@pytest.mark.asyncio
async def test_reporter_replays_after_process_restart(path: Path, tmp_path: Path) -> None:
    module = load_module(path, "reporter")
    database = tmp_path / f"{path.parts[-3]}.sqlite3"
    reporter = module.BackgroundReporter(
        "http://core.test",
        "instance-token",
        10,
        1.0,
        report_retry_backoff_seconds=0,
        spool_path=str(database),
    )
    reporter._spool.open()

    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Core offline", request=request)

    reporter._client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    reporter._spool_worker = asyncio.create_task(reporter._run_spool())
    assert reporter.enqueue(module.ReportItem("/v1/events", event_payload(), "restart-key"))
    deadline = time.monotonic() + 2
    while reporter.spool_status()["replay_failures"] == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert reporter.spool_status()["pending_records"] == 1
    await reporter.stop()

    replay = module.BackgroundReporter(
        "http://core.test",
        "instance-token",
        10,
        1.0,
        spool_path=str(database),
    )
    replay._spool.open()

    async def online(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        ingress = body["ingress"]
        return httpx.Response(
            201,
            request=request,
            json={
                "receipt_id": "receipt-replayed",
                "outcome": "committed",
                "spool_id": ingress["spool_id"],
                "sequence": ingress["sequence"],
                "record_sha256": ingress["record_sha256"],
            },
        )

    replay._client = httpx.AsyncClient(transport=httpx.MockTransport(online))
    replay._spool_worker = asyncio.create_task(replay._run_spool())
    replay._spool_wakeup.set()
    deadline = time.monotonic() + 3
    while replay.spool_status()["pending_records"] and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    status = replay.spool_status()
    assert status["pending_records"] == 0
    assert status["replay_successes"] == 1
    assert status["replay_failures"] >= 1
    await replay.stop()


@pytest.mark.asyncio
async def test_durable_spool_delivers_to_real_core_and_closes_watermark(
    app,
    tmp_path: Path,
) -> None:
    module = load_module(REPORTER_PATHS[0], "reporter")
    reporter = module.BackgroundReporter(
        "http://testserver",
        "lily-secret",
        10,
        1.0,
        spool_path=str(tmp_path / "core-delivery.sqlite3"),
    )
    reporter._spool.open()
    reporter._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )
    reporter._spool_worker = asyncio.create_task(reporter._run_spool())
    payload = event_payload(99)
    assert reporter.enqueue(module.ReportItem("/v1/events", payload, "core-delivery-key"))
    deadline = time.monotonic() + 3
    while reporter.spool_status()["pending_records"] and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    status = reporter.spool_status()
    assert status["pending_records"] == 0
    assert status["replay_successes"] == 1
    assert status["highest_sequence"] == 1

    admin = await reporter._client.get(
        "/v1/ingress/watermarks",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert admin.status_code == 200
    assert admin.json()[0]["spool_id"] == status["spool_id"]
    assert admin.json()[0]["highest_contiguous_sequence"] == 1
    await reporter.stop()
