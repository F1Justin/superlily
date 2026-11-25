import asyncio
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location("astra", Path("bridges/nekro/superlily_bridge/astra.py"))
astra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(astra)


async def compose(chat_key, origin, visible, answer, sources):
    return "莉莉答：" + answer, "test-lily", False


@pytest.mark.asyncio
async def test_native_notice_failure_does_not_repeat_charged_request(tmp_path, monkeypatch):
    client_type = httpx.AsyncClient
    calls = []
    sent = []
    delivered = asyncio.Event()
    key = tmp_path / "key"
    key.write_text("test-only")
    monkeypatch.setattr(astra, "KEY", key)
    snapshot = [
        {"type": "input_text", "text": "(msg_id:123) 测试"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]

    def provider(request):
        data = json.loads(request.content)
        assert data["input"][0]["content"][:-1] == snapshot
        assert "123" in data["input"][0]["content"][-1]["text"]
        assert data["instructions"] == astra.ASTRA_PROMPT
        assert data["tools"] == [{"type": "web_search"}]
        assert "max_tool_calls" not in data
        calls.append(request.url.path)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 500})
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "status": "completed",
                "usage": {"input_tokens": 500, "output_tokens": 128, "input_tokens_details": {}},
                "output": [{"content": [{"type": "output_text", "text": "答复"}]}],
            },
        )

    monkeypatch.setattr(
        astra.httpx, "AsyncClient", lambda **kw: client_type(transport=httpx.MockTransport(provider), **kw)
    )

    async def deliver(chat_key, origin, source, text, render):
        sent.append((origin, source, text))
        delivered.set()

    queue = astra.MathQueue(tmp_path / "queue.sqlite3", deliver, compose)
    queue.start()

    def submit():
        queue.submit(astra.YUNFASHU, "123", "source", snapshot)

    def send_text(chat, message):
        pass

    ctx = {"astra": submit, "send_msg_text": send_text, "_ck": "group"}
    try:
        with pytest.raises(SyntaxError):
            exec("astra()\nsend_msg_text(", ctx)
        with closing(sqlite3.connect(queue.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        with pytest.raises(TypeError):
            exec("astra()\nsend_msg_text(_ck)", ctx)
        exec("astra()\nsend_msg_text(_ck,'稍等')", ctx)
        await asyncio.wait_for(delivered.wait(), 2)
        submit()
        with closing(sqlite3.connect(queue.path)) as db:
            assert db.execute("SELECT state,payload,answer FROM questions").fetchall() == [
                ("done", None, None)
            ]
        assert calls == ["/v1/responses/input_tokens", "/v1/responses"]
        assert sent == [("123", "source", "莉莉答：答复")]
    finally:
        queue.task.cancel()
        await asyncio.gather(queue.task, return_exceptions=True)


def test_annotated_web_sources_are_numbered_and_deduplicated():
    url = "https://oeis.org/A000088"
    citation = {"type": "url_citation", "url": url}
    part = {"type": "output_text", "text": f"156 [OEIS]({url})", "annotations": [citation, citation]}
    assert astra.answer_with_sources([{"content": [part]}]) == ("156 [1]", f"[1] {url}")


@pytest.mark.asyncio
async def test_queue_keeps_processing_reaction_through_delivery_and_logs_once(tmp_path):
    started, delivering, finish, recorded = (asyncio.Event() for _ in range(4))
    marked, cleared, logs = [], [], []

    class Observer:
        def progress(self, chat_key, origin, stage):
            pass

        async def add_sparkle(self, chat_key, origin):
            marked.append(origin)

        async def clear_reaction(self, chat_key, origin):
            assert (chat_key, origin) not in queue.held_reactions
            cleared.append(origin)
            return True

        async def write_record(self, row):
            logs.append((row["origin"], row["answer"], row["final_answer"], row["state"]))
            recorded.set()

    async def generate(job_id, chat_key, origin, month, payload):
        started.set()
        with closing(queue.connect()) as db, db:
            db.execute("UPDATE questions SET answer='解答',state='ready' WHERE origin=?", (origin,))

    async def deliver(chat_key, origin, source, answer, render):
        delivering.set()
        await finish.wait()
        assert (chat_key, origin) in queue.held_reactions

    queue = astra.MathQueue(tmp_path / "queue", deliver, compose, Observer())
    queue.generate = generate
    queue.start()
    try:
        for _ in range(2):
            queue.submit(astra.YUNFASHU, "123", None, [{"type": "input_text", "text": "x"}])
        assert (astra.YUNFASHU, "123") in queue.held_reactions
        await asyncio.wait_for(delivering.wait(), 2)
        assert started.is_set() and (astra.YUNFASHU, "123") in queue.held_reactions
        finish.set()
        await asyncio.wait_for(recorded.wait(), 2)
        assert not await queue.finish_records()
        assert marked == cleared == ["123"]
        assert logs == [("123", "解答", "莉莉答：解答", "done")]
    finally:
        queue.task.cancel()
        await asyncio.gather(queue.task, return_exceptions=True)


def test_notice_is_required_in_the_same_script_and_chat():
    astra.require_notice('astra()\nsend_msg_text(_ck, "稍等")')
    for script in ("astra()", "astra()\nsend_msg_text(_ck)", 'astra()\nsend_msg_text(other, "稍等")'):
        with pytest.raises(ValueError):
            astra.require_notice(script)


@pytest.mark.asyncio
async def test_old_queue_and_equal_message_ids_in_two_groups(tmp_path):
    path = tmp_path / "queue"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE questions(origin TEXT PRIMARY KEY, month TEXT, state TEXT, usd REAL)")
        db.execute("INSERT INTO questions VALUES('old','2026-11','done',0.31)")

    received = []
    both = asyncio.Event()

    async def deliver(chat_key, origin, source, answer, render):
        received.append((chat_key, origin, answer))
        if len(received) == 2:
            both.set()

    queue = astra.MathQueue(path, deliver, compose)

    async def generate(job_id, chat_key, origin, month, payload):
        content = json.loads(payload)[0]["content"]
        with closing(queue.connect()) as db, db:
            db.execute(
                "UPDATE questions SET state='ready',answer=?,payload=NULL WHERE rowid=?",
                (content[0]["text"], job_id),
            )

    queue.generate = generate
    queue.start()
    try:
        groups = (astra.YUNFASHU, astra.TAOHUAYUAN)
        for chat in groups:
            queue.submit(chat, "123", None, [{"type": "input_text", "text": chat}])
        with pytest.raises(PermissionError):
            queue.submit("other", "123", None, [{"type": "input_text", "text": "private"}])
        await asyncio.wait_for(both.wait(), 2)
        assert received == [(chat, "123", "莉莉答：" + chat) for chat in groups]
        with closing(queue.connect()) as db:
            assert db.execute("SELECT chat_key,usd FROM questions WHERE origin='old'").fetchone() == (
                groups[0],
                0.31,
            )
    finally:
        queue.task.cancel()
        await asyncio.gather(queue.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_retry_and_restart_compose_saved_astra_result_without_regeneration(tmp_path):
    sent = []
    failed = asyncio.Event()
    done = asyncio.Event()

    async def broken(*args):
        failed.set()
        raise OSError("temporary")

    async def deliver(chat, origin, source, text, render):
        sent.append(text)
        done.set()

    queue = astra.MathQueue(tmp_path / "q", deliver, broken)
    queue.start()
    with closing(queue.connect()) as db, db:
        db.execute(
            "INSERT INTO questions(chat_key,origin,month,state,answer,payload) VALUES(?,?,?,'ready',?,?)",
            (
                astra.YUNFASHU,
                "123",
                "2026-11",
                "完整原稿",
                '[{"role":"user","content":[{"type":"input_text","text":"原题"}]}]',
            ),
        )
    await asyncio.wait_for(failed.wait(), 2)
    await queue.stop()
    with closing(queue.connect()) as db, db:
        assert db.execute("SELECT state,compose_attempts,answer FROM questions").fetchone() == (
            "ready",
            1,
            "完整原稿",
        )
        db.execute("UPDATE questions SET retry_at=0")
    queue = astra.MathQueue(queue.path, deliver, compose)
    queue.start()
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert sent == ["莉莉答：完整原稿"]
    finally:
        await queue.stop()


def test_lily_render_call_keeps_proof_and_surrounding_prose_without_running_code():
    filename = Path("bridges/nekro/superlily_bridge/astra_continuation.py")
    spec = importlib.util.spec_from_file_location("astra_continuation", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = "呜哇哇啊啊~\n\n```python\nproof = r'''$$\nx=1\n$$'''\nsubmit_rendered_markdown(proof)\n```\n呼噜呼噜~"
    assert module.reply_body(source) == ("呜哇哇啊啊~\n\n$$\nx=1\n$$\n\n呼噜呼噜~", True)
    assert module.reply_body("正文") == ("正文", False)
    with pytest.raises(ValueError):
        module.reply_body("submit_rendered_markdown(compute())")
