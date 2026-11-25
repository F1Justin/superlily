"""Durable, single-worker queue for delegated group answers."""

import ast
import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

YUNFASHU = "onebot_v11-group_1080353942"
TAOHUAYUAN = "onebot_v11-group_861651713"
ENABLED_CHATS = frozenset((YUNFASHU, TAOHUAYUAN))
ASTRA_PROMPT = "这是一段群聊对话，根据标明的触发消息、近期对话和图片，弄清群友实际在问什么，思考并组织答复。"
MODEL = "gpt-6.2-astra"
API = "https://api.openai.com/v1/responses"
OUTPUT_LIMIT = 16000
SEARCH_FEE_ESTIMATE_USD = 0.08
SEARCH_USD = 0.01
KEY = Path("/run/secrets/yunfashu-astra.key")
Deliver = Callable[[str, str, str | None, str, bool], Awaitable[None]]
Compose = Callable[[str, str, list[dict], str, str], Awaitable[tuple[str, str, bool]]]

SCHEMA = """CREATE TABLE IF NOT EXISTS questions(
    chat_key TEXT NOT NULL, origin TEXT NOT NULL, source TEXT, month TEXT NOT NULL, state TEXT NOT NULL,
    payload TEXT, answer TEXT, usd REAL NOT NULL DEFAULT 0, usage TEXT, response_id TEXT, error TEXT,
    sources TEXT, queued_at REAL, started_at REAL, generated_at REAL, ended_at REAL,
    release_reaction INTEGER DEFAULT 0, logged INTEGER DEFAULT 0, PRIMARY KEY(chat_key,origin))"""


def initialize_schema(db):
    db.execute("BEGIN IMMEDIATE")
    old = [col[1] for col in db.execute("PRAGMA table_info(questions)")]
    if old and "chat_key" not in old:
        db.execute("ALTER TABLE questions RENAME TO single_group_questions")
        db.execute(SCHEMA)
        fields = [col[1] for col in db.execute("PRAGMA table_info(questions)") if col[1] in old]
        names = ",".join(fields)
        db.execute(
            f"INSERT INTO questions(chat_key,{names}) SELECT ?,{names} FROM single_group_questions ORDER BY rowid",
            (YUNFASHU,),
        )
        db.execute("DROP TABLE single_group_questions")
    else:
        db.execute(SCHEMA)

    columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
    for name, kind in (
        ("final_answer", "TEXT"),
        ("render_final", "INTEGER DEFAULT 0"),
        ("lily_model", "TEXT"),
        ("compose_attempts", "INTEGER DEFAULT 0"),
        ("retry_at", "REAL DEFAULT 0"),
    ):
        if name not in columns:
            db.execute(f"ALTER TABLE questions ADD COLUMN {name} {kind}")


def require_notice(script: str) -> None:
    """Validate the two adjacent RPC calls before accepting a paid job."""
    statements = ast.parse(script).body
    for first, second in zip(statements, statements[1:]):
        if not isinstance(first, ast.Expr) or not isinstance(second, ast.Expr):
            continue
        start, notice = first.value, second.value
        if (
            isinstance(start, ast.Call)
            and isinstance(start.func, ast.Name)
            and start.func.id == "astra"
            and isinstance(notice, ast.Call)
            and isinstance(notice.func, ast.Name)
            and notice.func.id == "send_msg_text"
            and len(notice.args) == 2
            and isinstance(notice.args[0], ast.Name)
            and notice.args[0].id == "_ck"
        ):
            return
    raise ValueError("请在同一段脚本中紧接 astra() 调用 send_msg_text(_ck, ...)，给群友回一句。")


def cost(usage: dict) -> float:
    tokens = usage["input_tokens"]
    details = usage["input_tokens_details"]
    cached = details.get("cached_tokens", 0)
    writes = details.get("cache_write_tokens_details", {})
    short, long = writes.get("ttl_30m_tokens", 0), writes.get("ttl_12h_tokens", 0)
    inputs = (tokens - cached - short - long) * 8 + cached * 0.4 + short * 10 + long * 20
    return (
        inputs * (2 if tokens > 272000 else 1) + usage["output_tokens"] * (60 if tokens > 272000 else 40)
    ) / 1e6


def answer_with_sources(output: list[dict]) -> tuple[str, str]:
    paragraphs = []
    urls: dict[str, int] = {}
    for item in output:
        for part in item.get("content", []):
            if part.get("type") != "output_text":
                continue
            text = part["text"]
            for citation in part.get("annotations", []):
                if citation.get("type") != "url_citation":
                    continue
                url = citation.get("url", "")
                if not url.startswith(("https://", "http://")) or any(c.isspace() for c in url):
                    continue
                try:
                    if not urlsplit(url).hostname:
                        continue
                except ValueError:
                    continue
                number = urls.setdefault(url, len(urls) + 1)
                # Replace only a link with the exact annotated URL. Citation offsets
                # can refer to positions before the provider formatted the Markdown.
                pattern = r"\[[^\]\n]*\]\(" + re.escape(url) + r"\)"
                text = re.sub(pattern, f"[{number}]", text)
            paragraphs.append(text.strip())
    sources = "\n".join(f"[{number}] {url}" for url, number in urls.items())
    return "\n".join(paragraphs).strip(), sources


class MathQueue:
    def __init__(self, path: Path, deliver: Deliver, compose: Compose, observer=None):
        self.observer = observer
        self.compose = compose
        self.mark_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self.held_reactions: set[tuple[str, str]] = set()
        self.path = path
        self.deliver = deliver
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task | None = None

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            initialize_schema(db)
            self.path.chmod(0o600)
            # Never repeat a possibly charged generation or a possibly delivered QQ message.
            db.execute(
                "UPDATE questions SET state='uncertain',payload=NULL,error=state WHERE state IN ('calling','sending')"
            )
            self.held_reactions = {
                tuple(row)
                for row in db.execute(
                    "SELECT chat_key,origin FROM questions WHERE release_reaction=1 AND state IN ('queued','ready','composed','uncertain')"
                )
            }
        for chat_key, origin in self.held_reactions:
            self._schedule_sparkle(chat_key, origin)
        self.task = asyncio.create_task(self.run())

    def _schedule_sparkle(self, chat_key, origin):
        if self.observer:

            async def mark():
                for delay in (0, 1, 3):
                    if delay:
                        await asyncio.sleep(delay)
                    try:
                        await self.observer.add_sparkle(chat_key, origin)
                        return
                    except Exception as exc:
                        self.observer.progress(chat_key, origin, f"添加 ✨ 失败 ({type(exc).__name__})")

            self.mark_tasks[chat_key, origin] = asyncio.create_task(mark())

    async def stop(self):
        tasks = list(self.mark_tasks.values())
        if self.task:
            tasks.append(self.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def submit(self, chat_key: str, origin: str, source: str | None, visible: list[dict]):
        if chat_key not in ENABLED_CHATS:
            raise PermissionError("此群未启用深答")
        if not origin or not visible:
            raise ValueError("深答需要当前问题及其会话上下文")
        if not self.task or self.task.done():
            raise RuntimeError("深答服务尚未就绪")
        content = [*visible, {"type": "input_text", "text": "莉莉此次求助的触发消息 ID：" + origin}]
        payload = json.dumps([{"role": "user", "content": content}], ensure_ascii=False)
        with closing(self.connect()) as db, db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO questions(chat_key,origin,source,month,state,payload,queued_at,release_reaction) VALUES(?,?,?,?,'queued',?,?,?)",
                (
                    chat_key,
                    origin,
                    source,
                    datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m"),
                    payload,
                    time.time(),
                    int(self.observer is not None),
                ),
            ).rowcount
        if inserted and self.observer:
            self.held_reactions.add((chat_key, origin))
            self._schedule_sparkle(chat_key, origin)
            self.observer.progress(chat_key, origin, "已排队")
        self.wakeup.set()

    async def finish_records(self):
        if not self.observer:
            return False
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            pending = db.execute(
                "SELECT rowid AS job_id,* FROM questions WHERE state IN ('done','uncertain') "
                "AND ((state='done' AND release_reaction=1) OR logged=0) ORDER BY rowid"
            ).fetchall()
        retry = False
        for row in pending:
            job_id, chat_key, origin = row["job_id"], row["chat_key"], row["origin"]
            mark = self.mark_tasks.pop((chat_key, origin), None)
            if mark:
                await mark
            if row["release_reaction"] and row["state"] == "done":
                self.held_reactions.discard((chat_key, origin))
                try:
                    if await self.observer.clear_reaction(chat_key, origin):
                        with closing(self.connect()) as db, db:
                            db.execute("UPDATE questions SET release_reaction=0 WHERE rowid=?", (job_id,))
                    else:
                        retry = True
                except Exception as exc:
                    self.observer.progress(chat_key, origin, f"表情待清理 ({type(exc).__name__})")
                    retry = True
            if not row["logged"]:
                try:
                    await self.observer.write_record(row)
                    with closing(self.connect()) as db, db:
                        db.execute(
                            "UPDATE questions SET logged=1,answer=NULL,sources=NULL,final_answer=NULL,payload=NULL WHERE rowid=? AND state='done'",
                            (job_id,),
                        )
                        db.execute(
                            "UPDATE questions SET logged=1 WHERE rowid=? AND state='uncertain'", (job_id,)
                        )
                except Exception as exc:
                    self.observer.progress(chat_key, origin, f"执行日志待写入 ({type(exc).__name__})")
                    retry = True
        return retry

    async def run(self):
        while True:
            self.wakeup.clear()
            await self.finish_records()
            with closing(self.connect()) as db:
                row = db.execute(
                    "SELECT rowid,chat_key,origin,source,month,state,payload,answer,sources,final_answer,compose_attempts,render_final FROM questions "
                    "WHERE state IN ('queued','ready','composed') AND retry_at<=? ORDER BY rowid LIMIT 1",
                    (time.time(),),
                ).fetchone()
            if not row:
                try:
                    await asyncio.wait_for(self.wakeup.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            (
                job_id,
                chat_key,
                origin,
                source,
                month,
                state,
                payload,
                answer,
                sources,
                final_answer,
                attempts,
                render_final,
            ) = row
            try:
                if state == "queued":
                    if self.observer:
                        self.observer.progress(chat_key, origin, "正在调用")
                    await self.generate(job_id, chat_key, origin, month, payload)
                    continue
                if state == "ready":
                    visible = json.loads(payload)[0]["content"] if payload else []
                    if self.observer:
                        self.observer.progress(chat_key, origin, "莉莉正在组织答复")
                    final_answer, model, render_final = await self.compose(
                        chat_key, origin, visible, answer, sources or ""
                    )
                    if not final_answer.strip():
                        raise ValueError("莉莉续答为空")
                    with closing(self.connect()) as db, db:
                        db.execute(
                            "UPDATE questions SET state='composed',final_answer=?,lily_model=?,render_final=?,payload=NULL WHERE rowid=?",
                            (final_answer, model, render_final, job_id),
                        )
                    continue
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE questions SET state='sending' WHERE rowid=?", (job_id,))
                await self.deliver(chat_key, origin, source, final_answer, bool(render_final))
                with closing(self.connect()) as db, db:
                    db.execute(
                        "UPDATE questions SET state='done',ended_at=? WHERE rowid=?", (time.time(), job_id)
                    )
                    if not self.observer:
                        db.execute(
                            "UPDATE questions SET answer=NULL,sources=NULL,final_answer=NULL WHERE rowid=?",
                            (job_id,),
                        )
                if self.observer:
                    self.observer.progress(chat_key, origin, "投递完成")
            except Exception as exc:
                with closing(self.connect()) as db, db:
                    if state == "ready":
                        failed = attempts + 1 >= 3
                        db.execute(
                            "UPDATE questions SET state=?, compose_attempts=compose_attempts+1, retry_at=?, error=COALESCE(?,error) WHERE rowid=?",
                            (
                                "uncertain" if failed else "ready",
                                time.time() + 30,
                                type(exc).__name__ if failed else None,
                                job_id,
                            ),
                        )
                    elif state == "composed":
                        db.execute(
                            "UPDATE questions SET state='uncertain',error=?,ended_at=? WHERE rowid=?",
                            (type(exc).__name__, time.time(), job_id),
                        )
                    else:
                        db.execute(
                            "UPDATE questions SET state='ready',answer=?,error=? WHERE rowid=?",
                            ("这次深答没有完成，你可以引用原题再问我。", type(exc).__name__, job_id),
                        )

    async def generate(self, job_id, chat_key, origin, month, payload):
        headers = {"Authorization": "Bearer " + KEY.read_text().strip()}
        data = {
            "model": MODEL,
            "input": json.loads(payload),
            "instructions": ASTRA_PROMPT,
            "tools": [{"type": "web_search"}],
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(360, connect=10), trust_env=False) as client:
            count = await client.post(API + "/input_tokens", json=data, headers=headers)
            count.raise_for_status()
            inputs = int(count.json()["input_tokens"])
            # Allow headroom for retrieved pages before reconciling actual usage.
            estimated_inputs = inputs + 128_000
            long_context = estimated_inputs > 272_000
            reserve = (
                estimated_inputs * (20 if long_context else 10) + OUTPUT_LIMIT * (60 if long_context else 40)
            ) / 1e6 + SEARCH_FEE_ESTIMATE_USD
            with closing(self.connect()) as db, db:
                used = db.execute(
                    "SELECT COALESCE(SUM(usd),0) FROM questions WHERE month=?", (month,)
                ).fetchone()[0]
                if used + reserve > 100:
                    db.execute(
                        "UPDATE questions SET state='ready',error='budget',answer=? WHERE rowid=?",
                        ("本月深答剩余额度不足以完成这道题。", job_id),
                    )
                    return
                db.execute(
                    "UPDATE questions SET state='calling',usd=?,started_at=? WHERE rowid=?",
                    (reserve, time.time(), job_id),
                )
            request = {
                **data,
                "reasoning": {"effort": "high"},
                "max_output_tokens": OUTPUT_LIMIT,
                "store": False,
                "service_tier": "default",
            }
            response = await client.post(API, headers=headers, json=request)
            if 400 <= response.status_code < 500:
                with closing(self.connect()) as db, db:
                    db.execute("UPDATE questions SET usd=0 WHERE rowid=?", (job_id,))
            response.raise_for_status()
            result = response.json()
        usage = result["usage"]
        answer, sources = answer_with_sources(result["output"])
        actions = [
            item.get("action", {}).get("type")
            for item in result["output"]
            if item.get("type") == "web_search_call"
        ]
        searches = actions.count("search")
        usage["web_search_actions"] = actions
        usage["web_search_usd"] = searches * SEARCH_USD
        generated_at = time.time()
        error = None
        if result["status"] != "completed" or not answer:
            error = "生成未完成"
            sources = ""
            answer = "这次推导没有生成完整答案，你可以把想优先解决的一步告诉我。"
        with closing(self.connect()) as db, db:
            db.execute(
                "UPDATE questions SET state='ready',answer=?,sources=?,usd=?,usage=?,response_id=?,generated_at=?,error=? WHERE rowid=?",
                (
                    answer,
                    sources,
                    cost(usage) + usage["web_search_usd"],
                    json.dumps(usage),
                    result["id"],
                    generated_at,
                    error,
                    job_id,
                ),
            )
        if self.observer:
            try:
                await self.observer.save_prompt(chat_key, origin, request, result)
            except Exception as exc:
                self.observer.progress(chat_key, origin, f"请求详情保存失败 ({type(exc).__name__})")
