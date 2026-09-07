import asyncio
from contextlib import asynccontextmanager
from collections import namedtuple
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest
from sqlalchemy import select

from superlily_contracts import EventIn
from superlily_core.models import EventObservation, EventDecision, QQHistoryRecoveryProgress, IdentityNameObservation


ROOT = Path(__file__).parents[1]
PATHS = [ROOT / "bridges" / name for name in
         ("lily_nonebot/lily_core_bridge", "nekro/superlily_bridge")]


@pytest.fixture(params=PATHS)
def module(request, monkeypatch):
    name = "recovery_test_bridge"
    package = ModuleType(name)
    package.__path__ = [str(request.param)]
    monkeypatch.setitem(sys.modules, name, package)
    reporter = ModuleType(name + ".reporter")
    reporter.ReportItem = namedtuple("ReportItem", "endpoint payload idempotency_key")
    monkeypatch.setitem(sys.modules, name + ".reporter", reporter)
    for part in ("payloads", "history_recovery"):
        spec = importlib.util.spec_from_file_location(name + "." + part, request.param / (part + ".py"))
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, mod)
        spec.loader.exec_module(mod)
    return mod


def instance(_="9"):
    return {"instance_id": "lily-command", "platform": "qq", "adapter": "onebot_v11",
            "bot_id": "9", "role": "command"}


def raw(ts=100, seq=5, kind="group", sender=8):
    result = {"message_type": kind, "time": ts, "message_id": seq + 100,
              "real_seq": str(seq), "user_id": sender, "sender": {"nickname": "Known name"},
              "message": [{"type": "text", "data": {"text": "/status"}}]}
    if kind == "group":
        result["group_id"] = 7
    return result


class Reporter:
    def __init__(self):
        self.items = []
        self.accept = True

    def enqueue(self, item):
        if self.accept:
            self.items.append(item)
        return self.accept


class Bot:
    def __init__(self, *pages):
        self.pages = list(pages)
        self.calls = []

    async def call_api(self, api, **params):
        self.calls.append((api, params))
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def worker(module, tmp_path):
    reporter = Reporter()
    result = module.HistoryRecovery(str(tmp_path / "history.db"), reporter, instance, lambda: {}, interval=0)
    result.store = module.RecoveryStore(result.path)
    result.store.create("9", "group", "7", 90, 120)
    return result, reporter, result.store.jobs()[0]


def test_shared_implementation():
    assert (PATHS[0] / "history_recovery.py").read_bytes() == (PATHS[1] / "history_recovery.py").read_bytes()


def test_identity_and_private_peer(module):
    payload = module.history_message(raw(), instance(), "group", "7")
    EventIn.model_validate(payload)
    assert payload["occurred_at"] == module.iso(100)
    assert payload["metadata"]["observation_method"] == "onebot_history"
    with pytest.raises(ValueError, match="private_peer"):
        module.history_message(raw(kind="private", sender=6), instance(), "private", "8")
    missing = raw()
    missing.pop("real_seq")
    with pytest.raises(ValueError, match="native_identity"):
        module.history_message(missing, instance(), "group", "7")


async def test_page_crash_resume_and_repeat(module, tmp_path):
    task, reporter, job = worker(module, tmp_path)
    first = {"messages": [raw(110, 10), raw(100, 9)]}
    bot = Bot(first, {"messages": [raw(100, 9), raw(80, 8)]})
    reporter.accept = False
    with pytest.raises(RuntimeError, match="spool"):
        await task.page(bot, job)
    assert job["cursor"] is None and job["pages"] == 0
    reporter.accept = True
    bot = Bot(first, {"messages": [raw(100, 9), raw(80, 8)]})
    await task.page(bot, job)
    task.store.checkpoint(job, instance())
    pending_report = deepcopy(job["report"])
    task.store.db.close()
    task.store = module.RecoveryStore(task.path)
    job = task.store.jobs()[0]
    assert job["pages"] == 1 and job["report"] == pending_report
    assert task.report(job)
    await task.page(bot, job)
    assert bot.calls[-1][1]["message_seq"] == "109"
    assert job["state"] == "scanned"
    assert job["captured"] == 3  # Delivery deduplication belongs to the durable spool/Core.
    task.store.db.close()


async def test_stalled_page_and_identity_gap(module, tmp_path):
    task, reporter, job = worker(module, tmp_path)
    page = {"messages": [raw()]}
    await task.page(Bot(page), job)
    await task.page(Bot(page), job)
    assert job["state"] == "partial" and job["reason"] == "pagination_stalled"
    task.store.create("9", "group", "7", 90, 120)
    other = task.store.jobs()[-1]
    invalid = raw(95)
    invalid.pop("real_seq")
    await task.page(Bot({"messages": [invalid, raw(80)]}), other)
    assert other["state"] == "partial" and other["reason"] == "unverifiable_messages"
    task.store.db.close()


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("history_first", [False, True])
async def test_core_deduplicates_live_and_history_without_claim(module, client, app, kind, history_first, monkeypatch, tmp_path):
    from superlily_core import service
    from superlily_core.correlation import advisory_lock_key

    guard = service._correlation_guard

    @asynccontextmanager
    async def checked_guard(session, fingerprint):
        if fingerprint is not None:
            advisory_lock_key(fingerprint)
        async with guard(session, fingerprint):
            yield

    monkeypatch.setattr(service, "_correlation_guard", checked_guard)
    peer = "7" if kind == "group" else "8"
    history = module.history_message(raw(kind=kind), instance(), kind, peer)
    live = deepcopy(history)
    live["source_event_id"] = "live-message"
    live["metadata"].pop("observation_method")
    live["sender"]["account_name"] = "Live name"
    live["capture"] = {"status": "partial", "reason": "live_envelope", "omitted_fields": ["url"]}
    headers = {"Authorization": "Bearer lily-secret"}
    first, second = (history, live) if history_first else (live, history)
    spool_module = importlib.import_module(module.__package__ + ".spool")
    spool = spool_module.DurableIngressSpool(str(tmp_path / "core-spool.db"))
    spool.open()
    first_record = spool.append_event(first, "first-event")
    second_record = spool.append_event(second, "second-event")
    a = await client.post("/v1/events", json=first, headers={**headers, "Idempotency-Key": "first-event"})
    assert a.status_code == 201, a.text
    spool.acknowledge(first_record, a.json())
    if history_first:
        async with app.state.database.sessions() as session:
            assert not (await session.scalars(select(EventDecision))).all()
    b = await client.post("/v1/events", json=second, headers={**headers, "Idempotency-Key": "second-event"})
    assert b.status_code == 200, b.text
    spool.acknowledge(second_record, b.json())
    assert b.json()["highest_contiguous_sequence"] == 2
    replay = await client.post("/v1/events", json=first, headers={**headers, "Idempotency-Key": "first-event"})
    assert replay.status_code == 200, replay.text
    assert replay.json()["receipt_id"] == a.json()["receipt_id"]
    changed = deepcopy(first)
    changed["ingress"]["sequence"] = 3
    replay = await client.post("/v1/events", json=changed, headers={**headers, "Idempotency-Key": "first-event"})
    assert replay.status_code == 409
    claim = await client.post("/v1/claims/evaluate", json=history,
                              headers={**headers, "Idempotency-Key": "claim-event"})
    assert claim.status_code == 422
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(EventObservation))).all()) == 1
        assert len((await session.scalars(select(EventDecision))).all()) == 1
        if history_first:
            observation = (await session.scalars(select(EventObservation))).one()
            assert observation.metadata_json["live_observed"] is True
            assert observation.capture_reason == "live_envelope"
            assert observation.reported_source_event_id == "live-message"
            names = (await session.scalars(select(IdentityNameObservation))).all()
            assert {"onebot_history", "message"} <= {n.observation_method for n in names}
            for name in names:
                if name.observation_method == "onebot_history":
                    assert name.observed_at.year > 1970
    spool.close()


async def test_progress_contract_and_projection(module, tmp_path, client, app):
    task, reporter, job = worker(module, tmp_path)
    job["state"], job["reason"] = "partial", "history_exhausted"
    task.store.checkpoint(job, instance())
    task.report(job)
    item = reporter.items[-1]
    headers = {"Authorization": "Bearer lily-secret", "Idempotency-Key": item.idempotency_key}
    for code in (201, 200):
        response = await client.post("/v1/events", json=item.payload, headers=headers)
        assert response.status_code == code, response.text
    async with app.state.database.sessions() as session:
        progress = (await session.scalars(select(QQHistoryRecoveryProgress))).one()
        assert progress.state == "partial" and progress.reason == "history_exhausted"
        assert not (await session.scalars(select(EventDecision))).all()
    invalid = deepcopy(item.payload)
    invalid["metadata"]["history_recovery"]["reason"] = "changed"
    response = await client.post("/v1/events", json=invalid, headers=headers)
    assert response.status_code == 409
    invalid["metadata"]["history_recovery"]["state"] = "fully_recovered"
    response = await client.post("/v1/events", json=invalid, headers=headers)
    assert response.status_code == 422
    task.store.db.close()


async def test_discovery_degrades_and_retries_without_duplicate_children(module, tmp_path):
    task, _, _ = worker(module, tmp_path)
    task.store.create("9", "system", "9", 90, 120)
    parent = task.store.jobs()[-1]
    task.store.remember("9", "private", "8")
    with pytest.raises(RuntimeError, match="discovery_incomplete"):
        await task.discover(Bot([{"group_id": 7}], RuntimeError("unsupported"),
                                RuntimeError("unsupported")), parent)
    children = [j for j in task.store.jobs() if j["id"] != parent["id"]]
    assert {(j["kind"], j["peer"]) for j in children} == {("group", "7"), ("private", "8")}
    count = len(children)
    await task.discover(Bot([], [], []), parent)
    assert parent["state"] == "partial"
    assert len(task.store.jobs()) == count + 1
    task.store.db.close()


async def test_limits_and_timeout(module, tmp_path):
    task, _, job = worker(module, tmp_path)
    task.max_pages = 1
    await task.page(Bot({"messages": [raw()]}), job)
    assert job["reason"] == "page_limit"

    class SlowBot:
        async def call_api(self, *args, **kwargs):
            await asyncio.Event().wait()

    task.timeout = 0.01
    with pytest.raises(TimeoutError):
        await task.api(SlowBot(), "get_group_list")
    task.store.db.close()


async def test_connection_watermark_survives_restart(module, tmp_path):
    task, _, _ = worker(module, tmp_path)
    task.bots = lambda: {"9": object()}
    for _ in range(2):
        loop = asyncio.create_task(task.connections_loop())
        await asyncio.sleep(0.01)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        task.connections.clear()
    parents = [j for j in task.store.jobs() if j["kind"] == "system"]
    assert len(parents) == 2
    assert parents[0]["reason"] == "bootstrap_window"
    assert parents[1]["reason"] is None
    assert parents[1]["start"] >= parents[0]["end"] - 31
    task.store.db.close()


def test_history_envelopes_use_durable_spool_without_key_conflict(module, tmp_path):
    spool_module = importlib.import_module(module.__package__ + ".spool")
    spool = spool_module.DurableIngressSpool(str(tmp_path / "spool.db"))
    spool.open()
    first = module.history_message(raw(), instance(), "group", "7")
    record = spool.append_event(deepcopy(first), module.key(first["source_event_id"]))
    repeated = spool.append_event(deepcopy(first), module.key(first["source_event_id"]))
    assert repeated.sequence == record.sequence
    renamed = raw()
    renamed["sender"]["nickname"] = "Refreshed profile"
    second = module.history_message(renamed, instance(), "group", "7")
    assert second["source_event_id"] != first["source_event_id"]
    spool.append_event(second, module.key(second["source_event_id"]))
    spool.close()
