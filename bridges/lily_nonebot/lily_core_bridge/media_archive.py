"""Durable, bounded expansion and archival; never enters message execution."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import httpx

from .payloads import native_message_identity
from .reporter import ReportItem

MEDIA = {"forward", "node", "image", "record", "video", "file"}
HOSTS = {"multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn", "grouptalk.c2c.qq.com"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def platform_id(value):
    text = str(value) if value is not None else ""
    return text if re.fullmatch(r"[A-Za-z0-9_+=.@:-]{1,512}", text) and ":" not in text[:3] else None


def safe_url(value):
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname in HOSTS and parsed.port in {None, 443}
                and not parsed.username and not parsed.password and not parsed.fragment)
    except (ValueError, TypeError):
        return False


def segments(value):
    result, omitted = [], set()
    for segment in value[:128] if isinstance(value, list) else []:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            omitted.add("invalid_segment")
            continue
        data = {}
        for key, val in segment["data"].items():
            if key in {"text", "id", "qq", "name", "summary", "sub_type"} and isinstance(val, (str, int, bool)):
                if key in {"id", "qq"} and platform_id(val) is None:
                    omitted.add(key)
                else:
                    data[key] = val[:2000] if isinstance(val, str) else val
                    if isinstance(val, str) and len(val) > 2000:
                        omitted.add(key + ":truncated")
            else:
                omitted.add(str(key)[:64])
        result.append({"type": str(segment.get("type", "unknown"))[:64], "data": data})
    if isinstance(value, list) and len(value) > 128:
        omitted.add("segment_limit")
    return result, sorted(omitted)[:128]


class MediaArchive:
    def __init__(self, spool_path, reporter, bots, *, downloads=False, max_bytes=8_388_608,
                 max_items=64, max_depth=3, task_quota=134_217_728, interval=1.0):
        self.spool_path, self.reporter, self.bots = spool_path, reporter, bots
        self.downloads, self.max_bytes = downloads, max_bytes
        self.max_items, self.max_depth, self.task_quota, self.interval = max_items, max_depth, task_quota, interval
        self.db = self.source = self.task = self.client = None

    def start(self):
        path = Path(self.spool_path + ".media.sqlite3")
        self.source = sqlite3.connect(Path(self.spool_path).as_uri() + "?mode=ro", uri=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value INTEGER);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,body TEXT NOT NULL,active INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_media_active ON jobs(active);
        """)
        for candidate in (path, Path(str(path)+"-wal"), Path(str(path)+"-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)
        current = self.source.execute("SELECT coalesce(max(sequence),0) FROM spool_records").fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO state VALUES ('cursor',?)", (current,))
        self.client = httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False)
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.client:
            await self.client.aclose()
        for db in (self.source, self.db):
            if db:
                db.close()
        self.task = self.source = self.db = self.client = None

    def save(self, job):
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)",
                        (job["id"], json.dumps(job), int(not job["done"] or job["report"] is not None)))

    def scan(self):
        cursor = self.db.execute("SELECT value FROM state WHERE key='cursor'").fetchone()[0]
        rows = self.source.execute("SELECT sequence,payload_json FROM spool_records WHERE sequence>? ORDER BY sequence LIMIT 20", (cursor,)).fetchall()
        for sequence, raw in rows:
            if (self.db.execute("SELECT count(*) FROM jobs WHERE active=1").fetchone()[0] >= 128
                    or self.db.execute("PRAGMA page_count").fetchone()[0] * self.db.execute("PRAGMA page_size").fetchone()[0] >= self.task_quota):
                return
            payload = json.loads(raw)
            message = payload.get("message") or {}
            if message.get("id") and any(s.get("type") in MEDIA for s in message.get("segments", []) if isinstance(s, dict)):
                native = (payload.get("metadata") or {}).get("native_identity") or {}
                if not isinstance(native, dict):
                    native = {}
                native = {k: str(v)[:512] for k,v in native.items() if k in {"time", "user_id", "group_id", "real_seq", "msg_id", "msg_uid"}}
                identifier = digest([payload["instance"]["instance_id"], payload["conversation"]["type"], payload["conversation"]["id"], message["id"], native])
                if not self.db.execute("SELECT 1 FROM jobs WHERE id=?", (identifier,)).fetchone():
                    job = {"id": identifier, "instance": payload["instance"],
                           "conversation": {"type": payload["conversation"]["type"], "id": payload["conversation"]["id"]},
                           "source": payload["source_event_id"], "message": str(message["id"]), "native": native,
                           "attempts": 0, "retry_at": 0, "done": False, "report": None}
                    self.save(job)
            if sequence > cursor + 1:
                self.db.execute("INSERT INTO state VALUES ('source_gap_sequences',?) ON CONFLICT(key) DO UPDATE SET value=value+excluded.value", (sequence-cursor-1,))
            self.db.execute("UPDATE state SET value=? WHERE key='cursor'", (sequence,))
            cursor = sequence

    async def api(self, bot, name, **params):
        await asyncio.sleep(self.interval)
        return await asyncio.wait_for(bot.call_api(name, **params), 20)

    async def download(self, url):
        if not safe_url(url):
            raise ValueError("url_not_allowed")
        body = bytearray()
        async with self.client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code in {403, 404, 410}:
                raise ValueError("resource_expired")
            if response.status_code != 200:
                raise ValueError("download_http_error")
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("unsupported_encoding")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media_type):
                media_type = None
            length = response.headers.get("content-length")
            if length and int(length) > self.max_bytes:
                raise ValueError("file_size_limit")
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > self.max_bytes:
                    raise ValueError("file_size_limit")
                body.extend(chunk)
        sha = hashlib.sha256(body).hexdigest()
        response = await self.client.put(self.reporter.base_url + "/v1/qq-media/blobs/" + sha,
                                         content=bytes(body), headers={"Authorization": "Bearer " + self.reporter.token,
                                                                    "Content-Type": "application/octet-stream"})
        if response.status_code in {413, 507, 503}:
            raise ValueError({413:"file_size_limit", 507:"storage_quota", 503:"archive_storage_disabled"}[response.status_code])
        response.raise_for_status()
        result = response.json()
        if result.get("sha256") != sha or result.get("size_bytes") != len(body):
            raise ValueError("content_receipt_mismatch")
        return sha, len(body), media_type

    async def collect(self, bot, job):
        job["_items"] = []
        raw = await self.api(bot, "get_msg", message_id=job["message"])
        if not isinstance(raw, dict) or not isinstance(raw.get("message"), list):
            raise ValueError("message_unavailable")
        native = native_message_identity(raw)
        if not job["native"].get("time") or native.get("time") != job["native"]["time"]:
            raise ValueError("parent_identity_unverified")
        if any(native.get(k) is not None and native[k] != v for k,v in job["native"].items()):
            raise ValueError("parent_identity_mismatch")
        items, text_budget = job["_items"], [65536]

        async def walk(parts, prefix, depth, ancestors):
            if not isinstance(parts, list):
                raise ValueError("invalid_segments")
            for index, part in enumerate(parts):
                if len(items) >= self.max_items:
                    items[-1]["reason"] = "item_limit"
                    return
                if not isinstance(part, dict) or part.get("type") not in MEDIA:
                    continue
                kind, data = part["type"], part.get("data") or {}
                path = prefix + str(index)
                item = {"path": path, "kind": kind, "state": "observed"}
                items.append(item)
                if not isinstance(data, dict):
                    item.update(state="failed", reason="invalid_segment")
                    continue
                identifier = platform_id(data.get("id") if kind == "forward" else data.get("file_id") or data.get("file"))
                item["platform_id"] = identifier
                try:
                    if kind in {"forward", "node"}:
                        if depth >= self.max_depth:
                            item.update(state="limited", reason="depth_limit")
                            continue
                        if kind == "forward":
                            if identifier in ancestors:
                                item.update(state="limited", reason="forward_cycle")
                                continue
                            if not identifier:
                                raise ValueError("missing_forward_id")
                            result = await self.api(bot, "get_forward_msg", message_id=identifier)
                            nodes = result.get("messages") if isinstance(result, dict) else None
                        else:
                            nodes = [data]
                        if not isinstance(nodes, list) or not nodes:
                            raise ValueError("forward_unavailable")
                        item["state"] = "expanded"
                        for n, node in enumerate(nodes):
                            if len(items) >= self.max_items:
                                item.update(state="limited", reason="item_limit")
                                break
                            if isinstance(node, dict) and node.get("type") == "node":
                                node = node.get("data")
                            if not isinstance(node, dict):
                                item.update(state="limited", reason="invalid_node")
                                continue
                            content = node.get("message") if isinstance(node.get("message"), list) else node.get("content")
                            clean, omitted = segments(content)
                            for segment in clean:
                                if "text" in segment["data"]:
                                    value = str(segment["data"]["text"])
                                    segment["data"]["text"] = value[:text_budget[0]]
                                    text_budget[0] = max(0, text_budget[0]-len(value))
                            sender = node.get("sender") if isinstance(node.get("sender"), dict) else {}
                            timestamp = node.get("time")
                            occurred = None
                            if type(timestamp) in {int, float} and math.isfinite(timestamp) and 0 < timestamp < 253402300800:
                                occurred = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                            entry = {"path": path + "." + str(n), "kind": "node", "state": "observed",
                                     "sender_id": platform_id(node.get("user_id") or sender.get("user_id")),
                                     "sender_name": str(sender.get("nickname") or node.get("nickname") or "")[:512] or None,
                                     "occurred_at": occurred, "segments": clean, "omitted_fields": omitted,
                                     "text": "".join(str(s["data"].get("text", "")) for s in clean)[:16000] or None,
                                     "reason": None if occurred else "node_time_missing"}
                            if not isinstance(content, list):
                                entry.update(state="unavailable", reason="node_content_missing")
                            elif omitted or text_budget[0] == 0:
                                entry["reason"] = "node_fields_omitted"
                            items.append(entry)
                            await walk(content if isinstance(content, list) else [], entry["path"] + ".", depth+1, ancestors | {identifier})
                        continue
                    item["name"] = str(data.get("name") or data.get("file_name") or "")[:512] or None
                    size = data.get("file_size") or data.get("size")
                    item["size_bytes"] = int(size) if str(size).isdigit() else None
                    if item["size_bytes"] is not None and item["size_bytes"] > 9_223_372_036_854_775_807:
                        item.update(state="limited", reason="file_size_limit", size_bytes=None)
                        continue
                    if item["size_bytes"] is not None and item["size_bytes"] > self.max_bytes:
                        item.update(state="limited", reason="file_size_limit")
                        continue
                    if not self.downloads:
                        item.update(state="metadata_only", reason="downloads_disabled")
                        continue
                    url = data.get("url") or (data.get("file") if safe_url(data.get("file")) else None)
                    if kind == "file" and identifier and depth > 0 and not url:
                        raise ValueError("file_context_unknown")
                    if kind == "file" and identifier and depth == 0:
                        params = {"file_id": identifier}
                        api = "get_private_file_url"
                        if job["conversation"]["type"] == "group":
                            api = "get_group_file_url"
                            params["group_id"] = job["conversation"]["id"]
                        result = await self.api(bot, api, **params)
                        url = result.get("url") if isinstance(result, dict) else None
                    sha, size, media_type = await self.download(url)
                    item.update(state="archived", sha256=sha, size_bytes=size, media_type=media_type)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                        "url_not_allowed", "resource_expired", "file_size_limit", "forward_unavailable",
                        "missing_forward_id", "content_receipt_mismatch", "download_http_error",
                        "storage_quota", "archive_storage_disabled", "unsupported_encoding", "file_context_unknown"} else type(exc).__name__
                    item.update(state="limited" if reason in {"url_not_allowed", "file_size_limit", "storage_quota", "unsupported_encoding", "file_context_unknown"} else "unavailable", reason=reason)
        await walk(raw["message"], "", 0, set())
        return items

    def report(self, job):
        if job["report"]:
            payload = job["report"]
            if not self.reporter.enqueue(ReportItem("/v1/events", payload, digest([job["instance"]["instance_id"],payload["source_event_id"]]))):
                return False
            job["report"] = None
            self.save(job)
        return True

    async def run(self):
        while True:
            try:
                self.scan()
                for (body,) in self.db.execute("SELECT body FROM jobs WHERE active=1 ORDER BY rowid LIMIT 128").fetchall():
                    job = json.loads(body)
                    if not self.report(job):
                        break
                    if job["done"] or job["retry_at"] > time.time():
                        continue
                    bot = self.bots().get(str(job["instance"]["bot_id"]))
                    if bot is None:
                        continue
                    try:
                        items = await asyncio.wait_for(self.collect(bot, job), 90)
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in {
                            "parent_identity_unverified", "parent_identity_mismatch", "message_unavailable", "invalid_segments"
                        } else type(exc).__name__
                        items = job.get("_items", []) + [{"path":"999999", "kind":"unknown", "state":"failed", "reason":reason}]
                    job.pop("_items", None)
                    job["attempts"] += 1
                    retry = any(i["state"] in {"failed", "unavailable"} for i in items)
                    job["done"] = not retry or job["attempts"] >= 3
                    job["retry_at"] = time.time()+60
                    partial = any(i.get("reason") or i["state"] in {"limited", "failed", "unavailable"} for i in items)
                    report = {"job_id":job["id"], "revision":job["attempts"], "parent_source_event_id":job["source"],
                              "parent_message_id":job["message"], "state":"partial" if partial else "complete", "items":items}
                    while len(json.dumps(report).encode()) > 524288 and report["items"]:
                        report["items"].pop()
                        report.update(state="partial", reason="report_size_limit")
                    job["report"] = {"schema_version":"1.0", "source_event_id": "media:"+job["id"]+":"+str(job["attempts"]),
                        "instance":job["instance"], "event_type":"audit.qq_media_archive", "conversation":job["conversation"],
                        "occurred_at":datetime.now(timezone.utc).isoformat(), "metadata":{"qq_media_archive":report},
                        "references":[{"type":"derived_from", "platform_message_id":job["message"],
                                       "source_event_id":job["source"], "conversation_type":job["conversation"]["type"],
                                       "conversation_id":job["conversation"]["id"]}]}
                    self.save(job)
                    if not self.report(job):
                        break
            except Exception:
                logging.getLogger(__name__).exception("QQ media archive worker iteration failed")
            await asyncio.sleep(2)
