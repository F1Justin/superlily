import asyncio
from collections import namedtuple
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import httpx
import pytest
from sqlalchemy import select

from superlily_contracts import EventIn
from superlily_contracts.media_archive import QQMediaReport
from superlily_core.models import EventDecision, QQMediaArchiveItem, QQMediaBlob

ROOT = Path(__file__).parents[1]
PATHS = [ROOT / "bridges" / name for name in ("lily_nonebot/lily_core_bridge", "nekro/superlily_bridge")]


@pytest.fixture(params=PATHS)
def bridge(request, monkeypatch):
    name = "media_test_bridge"
    package = ModuleType(name)
    package.__path__ = [str(request.param)]
    monkeypatch.setitem(sys.modules, name, package)
    reporter = ModuleType(name + ".reporter")
    reporter.ReportItem = namedtuple("ReportItem", "endpoint payload idempotency_key")
    monkeypatch.setitem(sys.modules, name + ".reporter", reporter)
    for part in ("payloads", "spool", "media_archive"):
        spec = importlib.util.spec_from_file_location(name + "." + part, request.param / (part + ".py"))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        spec.loader.exec_module(module)
    return module


def parent(parts=None):
    return {"schema_version":"1.0", "source_event_id":"parent-message", "event_type":"message",
            "instance":{"instance_id":"lily-command", "platform":"qq", "adapter":"onebot_v11", "bot_id":"9", "role":"command"},
            "conversation":{"type":"group", "id":"7"}, "sender":{"id":"8"},
            "message":{"id":"105", "segments":parts or [{"type":"image", "data":{"file":"sample.jpg"}}]},
            "occurred_at":datetime.fromtimestamp(100,timezone.utc).isoformat(),
            "metadata":{"native_identity":{"time":"100", "user_id":"8", "group_id":"7", "real_seq":"5"}}}


def raw(parts):
    return {"message_type":"group", "group_id":7, "user_id":8, "time":100,
            "message_id":105, "real_seq":5, "message":parts}


def job(payload=None):
    payload = payload or parent()
    return {"id":"a"*64, "instance":payload["instance"], "conversation":payload["conversation"],
            "source":payload["source_event_id"], "message":"105", "native":payload["metadata"]["native_identity"],
            "attempts":0, "retry_at":0, "done":False, "report":None}


class Reporter:
    base_url = "http://core.test"
    token = "lily-secret"

    def __init__(self, spool=None):
        self.spool, self.items, self.accept = spool, [], True

    def enqueue(self, item):
        if not self.accept:
            return False
        self.items.append(deepcopy(item))
        if self.spool:
            self.spool.append_event(item.payload, item.idempotency_key)
        return True


class Bot:
    def __init__(self, root, forwards=None):
        self.root, self.forwards, self.calls = root, forwards or {}, []

    async def call_api(self, name, **params):
        self.calls.append(name)
        if name == "get_msg":
            return self.root
        if name == "get_forward_msg":
            return {"messages":self.forwards[params["message_id"]]}
        raise RuntimeError("unavailable")


def event(items):
    payload = parent()
    return {"schema_version":"1.0", "source_event_id":"media-test-report", "instance":payload["instance"],
            "event_type":"audit.qq_media_archive", "conversation":payload["conversation"],
            "occurred_at":datetime.now(timezone.utc).isoformat(),
            "metadata":{"qq_media_archive":{"job_id":"a"*64, "revision":1,
                "parent_source_event_id":"parent-message", "parent_message_id":"105", "state":"complete", "items":items}}}


def test_implementations_match():
    assert (PATHS[0]/"media_archive.py").read_text().strip() == (PATHS[1]/"media_archive.py").read_text().strip()


def test_url_and_segment_boundaries(bridge):
    assert bridge.safe_url("https://multimedia.nt.qq.com.cn/download?signature=temporary")
    for url in ("http://gchat.qpic.cn/a", "https://127.0.0.1/a", "https://gchat.qpic.cn.evil.test/a",
                "https://user:pass@gchat.qpic.cn/a", "https://gchat.qpic.cn:8443/a", "file:///etc/passwd", None):
        assert not bridge.safe_url(url)
    cleaned, omitted = bridge.segments([{"type":"image", "data":{"url":"https://secret", "token":"secret", "id":"sample"}}])
    assert "secret" not in json.dumps(cleaned)
    assert omitted == ["token", "url"]


async def test_nested_nodes_cycle_and_missing_time(bridge):
    root = raw([{"type":"forward", "data":{"id":"A"}}])
    forwards = {"A":[{"sender":{"nickname":"Quoted", "user_id":8}, "time":42,
        "message":[{"type":"text", "data":{"text":"hello"}}, {"type":"forward", "data":{"id":"B"}}]}],
        "B":[{"type":"node", "data":{"user_id":8, "nickname":"Older", "content":[{"type":"forward", "data":{"id":"A"}}]}}]}
    task = bridge.MediaArchive("unused", Reporter(), lambda:{}, max_depth=5, interval=0)
    items = await task.collect(Bot(root, forwards), job())
    QQMediaReport.model_validate(event(items)["metadata"]["qq_media_archive"])
    assert [i["sender_name"] for i in items if i["kind"] == "node"] == ["Quoted", "Older"]
    assert next(i for i in items if i.get("sender_name") == "Older")["occurred_at"] is None
    assert any(i.get("reason") == "forward_cycle" for i in items)
    assert len({i["path"] for i in items}) == len(items)
    task.max_depth = 0
    assert (await task.collect(Bot(root, forwards), job()))[0]["reason"] == "depth_limit"


async def test_content_archival_dedup_and_offline_read(bridge, app, client, tmp_path):
    app.state.settings = replace(app.state.settings, qq_media_root=str(tmp_path/"media"), qq_media_max_bytes=64, qq_media_quota_bytes=64)
    body = b"archived image fixture"
    sha = hashlib.sha256(body).hexdigest()
    asgi = httpx.ASGITransport(app=app)
    media_calls = []

    async def transport(request):
        if request.url.host == "core.test":
            return await asgi.handle_async_request(request)
        assert "authorization" not in request.headers
        media_calls.append(str(request.url))
        return httpx.Response(200, content=body)

    task = bridge.MediaArchive("unused", Reporter(), lambda:{}, downloads=True, interval=0, max_bytes=64)
    task.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    parts = [{"type":"image", "data":{"file":"same.jpg", "url":"https://gchat.qpic.cn/image?token=TEMP"}}]*2
    items = await task.collect(Bot(raw(parts)), job())
    await task.client.aclose()
    assert [i["state"] for i in items] == ["archived", "archived"]
    assert "TEMP" not in json.dumps(items)
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(QQMediaBlob))).all()) == 1
    payload = event(items)
    headers = {"Authorization":"Bearer lily-secret", "Idempotency-Key":"media-report-1"}
    for code in (201, 200):
        result = await client.post("/v1/events", json=payload, headers=headers)
        assert result.status_code == code, result.text
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(QQMediaArchiveItem))).all()) == 2
        assert not (await session.scalars(select(EventDecision))).all()
    assert (await client.get("/v1/qq-media/blobs/"+sha)).status_code == 401
    downloaded = await client.get("/v1/qq-media/blobs/"+sha, headers={"Authorization":"Bearer admin-secret"})
    assert downloaded.content == body
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert len(media_calls) == 2
    changed = deepcopy(payload)
    changed["metadata"]["qq_media_archive"]["items"][0]["name"] = "changed"
    assert (await client.post("/v1/events", json=changed, headers=headers)).status_code == 409
    claim = await client.post("/v1/claims/evaluate", json=payload, headers=headers)
    assert claim.status_code == 422


async def test_limits_expiry_and_parent_reuse(bridge):
    task = bridge.MediaArchive("unused", Reporter(), lambda:{}, downloads=True, interval=0, max_bytes=4)
    for status, data, expected in [(200,b"12345","file_size_limit"), (403,b"","resource_expired"), (302,b"","download_http_error")]:
        task.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, content=data)))
        items = await task.collect(Bot(raw([{"type":"image", "data":{"url":"https://gchat.qpic.cn/a"}}])),job())
        assert items[0]["reason"] == expected
        await task.client.aclose()
    changed = raw([])
    changed["time"] = 101
    with pytest.raises(ValueError, match="identity"):
        await task.collect(Bot(changed),job())


async def test_item_budget_preserves_archived_reference(bridge):
    task = bridge.MediaArchive("unused", Reporter(), lambda:{}, downloads=True, interval=0, max_items=1)

    async def download(url):
        return "b"*64, 10, "image/png"

    task.download = download
    items = await task.collect(Bot(raw([{"type":"image", "data":{}}]*2)), job())
    assert len(items) == 1
    assert items[0]["state"] == "archived" and items[0]["sha256"] == "b"*64
    assert items[0]["reason"] == "item_limit"
    QQMediaReport.model_validate(event(items)["metadata"]["qq_media_archive"])


async def test_file_url_lookup_and_encoded_content_rejection(bridge):
    task = bridge.MediaArchive("unused", Reporter(), lambda:{}, downloads=True, interval=0)
    task.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"Content-Encoding":"gzip"}, content=b"")))
    calls = []

    class FileBot(Bot):
        async def call_api(self, name, **params):
            if name == "get_msg":
                return self.root
            calls.append((name, params))
            return {"url":"https://gchat.qpic.cn/file?token=TEMP"}

    for conversation, expected in (("group", "get_group_file_url"), ("private", "get_private_file_url")):
        current = job()
        current["conversation"]["type"] = conversation
        items = await task.collect(FileBot(raw([{"type":"file", "data":{"file_id":"file-id"}}])), current)
        assert calls[-1][0] == expected
        assert ("group_id" in calls[-1][1]) == (conversation == "group")
        assert items[0]["reason"] == "unsupported_encoding"
        assert "TEMP" not in json.dumps(items)
    calls.clear()
    nested = raw([{"type":"node", "data":{"content":[{"type":"file", "data":{"file_id":"file-id"}}]}}])
    items = await task.collect(FileBot(nested), job())
    assert calls == []
    assert items[-1]["reason"] == "file_context_unknown"
    await task.client.aclose()


async def test_blob_validation_and_quota(app, client, tmp_path):
    app.state.settings = replace(app.state.settings, qq_media_root=str(tmp_path/"media"), qq_media_max_bytes=4, qq_media_quota_bytes=4)
    headers = {"Authorization":"Bearer lily-secret"}
    path = "/v1/qq-media/blobs/"
    await app.state.qq_media_upload_slots.acquire()
    await app.state.qq_media_upload_slots.acquire()
    assert (await client.put(path+"a"*64, content=b"x", headers=headers)).status_code == 429
    app.state.qq_media_upload_slots.release()
    app.state.qq_media_upload_slots.release()
    assert (await client.put(path+"a"*64, content=b"wrong", headers=headers)).status_code == 413
    assert (await client.put(path+"a"*64, content=b"x", headers=headers)).status_code == 422
    for data, expected in [(b"1234",200), (b"1234",200), (b"a",507)]:
        response = await client.put(path+hashlib.sha256(data).hexdigest(), content=data, headers=headers)
        assert response.status_code == expected, response.text
    payload = event([{"path":"0", "kind":"image", "state":"archived", "size_bytes":4, "sha256":hashlib.sha256(b"1234").hexdigest()}])
    payload["instance"].update(instance_id="nekro-agent", bot_id="10", role="agent")
    assert (await client.post("/v1/events", json=payload, headers={"Authorization":"Bearer nekro-secret", "Idempotency-Key":"cross-instance"})).status_code == 422
    response = await client.put(path+hashlib.sha256(b"1234").hexdigest(), content=b"1234",
                                headers={"Authorization":"Bearer nekro-secret"})
    assert response.status_code == 200
    assert len([p for p in (tmp_path/"media").iterdir() if not p.name.startswith(".")]) == 1


async def test_large_forward_projection_replay_is_lossless(client, app):
    payload = event([{"path":str(i), "kind":"node", "state":"observed", "text":"字"*16000,
                      "segments":[{"type":"text", "data":{"text":"字"*16000}}]} for i in range(3)])
    headers = {"Authorization":"Bearer lily-secret", "Idempotency-Key":"large-forward-report"}
    for code in (201, 200):
        response = await client.post("/v1/events", json=payload, headers=headers)
        assert response.status_code == code, response.text
    async with app.state.database.sessions() as session:
        items = (await session.scalars(select(QQMediaArchiveItem))).all()
        assert len(items) == 3 and all(len(i.text) == 16000 for i in items)


async def test_durable_scan_and_pending_report_restart(bridge, tmp_path):
    spool_type = sys.modules[bridge.__package__+".spool"].DurableIngressSpool
    spool = spool_type(str(tmp_path/"source.db"))
    spool.open()
    reporter = Reporter(spool)
    reporter.accept = False
    parts = [{"type":"image", "data":{"file":"sample.jpg", "url":"https://gchat.qpic.cn/a?token=TEMP"}}]
    task = bridge.MediaArchive(str(tmp_path/"source.db"), reporter, lambda:{"9":Bot(raw(parts))}, interval=0)
    task.start()
    spool.append_event(parent(parts),"parent-spool")
    for _ in range(100):
        await asyncio.sleep(0.01)
        row = task.db.execute("SELECT body FROM jobs").fetchone()
        if row and json.loads(row[0])["report"]:
            break
    saved = json.loads(row[0])
    assert saved["report"] and saved["done"]
    assert "TEMP" not in json.dumps(saved)
    await task.stop()
    reporter.accept = True
    task.start()
    for _ in range(100):
        await asyncio.sleep(0.01)
        if reporter.items:
            break
    assert reporter.items[0].payload == saved["report"]
    assert json.loads(task.db.execute("SELECT body FROM jobs").fetchone()[0])["attempts"] == 1
    EventIn.model_validate(reporter.items[0].payload)
    await task.stop()
    spool.close()


async def test_failed_job_retries_are_bounded(bridge, tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr(bridge.time, "time", lambda:clock[0])
    spool = sys.modules[bridge.__package__+".spool"].DurableIngressSpool(str(tmp_path/"source.db"))
    spool.open()
    reporter = Reporter()
    task = bridge.MediaArchive(str(tmp_path/"source.db"), reporter, lambda:{"9":Bot({})}, interval=0)
    task.start()
    spool.append_event(parent(),"parent-spool")
    for attempt in range(1,4):
        for _ in range(300):
            await asyncio.sleep(0.01)
            if len(reporter.items) >= attempt:
                break
        assert len(reporter.items) == attempt
        clock[0] += 61
    stored = json.loads(task.db.execute("SELECT body FROM jobs").fetchone()[0])
    assert stored["done"] and stored["attempts"] == 3
    assert [i.payload["metadata"]["qq_media_archive"]["revision"] for i in reporter.items] == [1,2,3]
    assert all(i.payload["metadata"]["qq_media_archive"]["state"] == "partial" for i in reporter.items)
    await task.stop()
    spool.close()


async def test_private_storage_and_symlink_boundaries(app, client, tmp_path):
    root = tmp_path/"media"
    root.mkdir(mode=0o700)
    app.state.settings = replace(app.state.settings, qq_media_root=str(root))
    content = tmp_path/"outside"
    content.write_bytes(b"outside")
    sha = hashlib.sha256(b"outside").hexdigest()
    (root/sha).symlink_to(content)
    assert (await client.get("/v1/qq-media/blobs/"+sha, headers={"Authorization":"Bearer admin-secret"})).status_code == 404
    root.chmod(0o777)
    assert (await client.put("/v1/qq-media/blobs/"+sha, content=b"outside", headers={"Authorization":"Bearer lily-secret"})).status_code == 503
