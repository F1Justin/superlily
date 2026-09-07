"""Bounded OneBot history recovery with durable cursors and evidence."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4, uuid5, NAMESPACE_URL

from .payloads import native_message_identity, message_source_event_id, message_references
from .reporter import ReportItem


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def history_message(raw, instance, kind, peer):
    if not isinstance(raw, dict) or raw.get("message_type") != kind:
        raise ValueError("invalid_message_type")
    timestamp = raw.get("time")
    if (isinstance(timestamp, bool) or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp) or timestamp <= 0):
        raise ValueError("missing_platform_time")
    sender = str(raw.get("user_id", ""))
    if not sender.isdigit() or raw.get("message_id") is None:
        raise ValueError("missing_message_identity")
    if kind == "group" and str(raw.get("group_id")) != peer:
        raise ValueError("group_mismatch")
    if kind == "private" and sender not in {peer, instance["bot_id"]}:
        raise ValueError("private_peer_mismatch")
    native = native_message_identity(raw)
    if not any(native.get(field) for field in ("real_seq", "msg_id", "msg_uid")):
        raise ValueError("insufficient_native_identity")
    message = raw.get("message")
    if not isinstance(message, list):
        raise ValueError("array_message_required")
    if len(message) > 128:
        raise ValueError("message_too_large")
    segments, attachments, texts, omitted = [], [], [], set()
    for segment in message:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            raise ValueError("invalid_segment")
        typ, data = str(segment.get("type", "unknown")), segment["data"]
        # Metadata/temporary credentials are not needed to establish message identity.
        safe = {k: v for k, v in data.items()
                if k in {"text", "id", "qq", "file", "name", "file_name", "file_size", "summary", "sub_type", "data"}
                and isinstance(v, (str, int, bool))}
        for field in ("file",):
            if isinstance(safe.get(field), str) and ("://" in safe[field] or safe[field].startswith("/")):
                safe.pop(field)
        omitted.update(str(k)[:64] for k in data if k not in safe)
        segments.append({"type": typ, "data": safe})
        if typ == "text":
            texts.append(str(safe.get("text", "")))
        if typ in {"image", "record", "video", "file"}:
            attachments.append({"type": typ, "platform_id": safe.get("file"),
                                "name": safe.get("name") or safe.get("file_name"),
                                "size_bytes": safe.get("file_size") if type(safe.get("file_size")) is int else None})
    conv = {"type": kind, "id": peer, "name": None}
    source = "history:" + message_source_event_id(conv, raw["message_id"], native,
                                                  sender_id=sender, occurred_at=iso(timestamp))
    profile = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
    nickname = str(profile["nickname"])[:512] if profile.get("nickname") else None
    card = str(profile["card"])[:512] if profile.get("card") else nickname
    payload = {"schema_version": "1.0", "source_event_id": source, "instance": instance,
            "event_type": "message", "conversation": conv,
            "sender": {"id": sender, "account_name": nickname, "display_name": card, "name": card, "roles": []},
            "message": {"id": str(raw["message_id"]), "text": "".join(texts) or None,
                        "segments": segments, "attachments": attachments},
            "references": message_references(segments, conv), "occurred_at": iso(timestamp), "raw": None,
            "metadata": {"observation_method": "onebot_history", "native_identity": native,
                         "history_omitted_segment_fields": sorted(omitted)[:128],
                         "history_message_sha256": key(json.dumps(message, sort_keys=True, ensure_ascii=False))}}
    # The platform can refresh names or segment data between reads. Distinct
    # envelopes need distinct spool keys; Core still deduplicates native identity.
    payload["source_event_id"] += ":" + key(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return payload


class RecoveryStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS online(bot TEXT PRIMARY KEY, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS peers(bot TEXT, kind TEXT, peer TEXT, PRIMARY KEY(bot,kind,peer));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, body TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_recovery_active ON jobs(active);
        """)
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                os.chmod(candidate, 0o600)

    def save(self, job):
        active = job["state"] in {"pending", "running"} or job.get("report") is not None
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (job["id"], json.dumps(job), int(active)))

    def jobs(self, *, active_only=False):
        query = "SELECT body FROM jobs" + (" WHERE active=1" if active_only else "") + " ORDER BY rowid LIMIT 1000"
        return [json.loads(row["body"]) for row in self.db.execute(query)]

    def remember(self, bot, kind, peer):
        if kind in {"group", "private"} and str(peer).isdigit():
            self.db.execute("INSERT OR IGNORE INTO peers VALUES (?,?,?)", (bot, kind, str(peer)))

    def create(self, bot, kind, peer, start, end, reason=None, job_id=None):
        job_id = job_id or str(uuid4())
        if self.db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
            return
        job = {"id": job_id, "bot": bot, "kind": kind, "peer": peer, "start": start, "end": end,
               "state": "pending", "pages": 0, "captured": 0, "rejected": 0, "attempts": 0,
               "cursor": None, "oldest": None, "seen": [], "reason": reason, "revision": 0,
               "report": None, "retry_at": 0}
        self.save(job)

    def checkpoint(self, job, instance):
        job["revision"] += 1
        progress = {"job_id": job["id"], "revision": job["revision"], "window_start": iso(job["start"]),
                    "window_end": iso(job["end"]), "state": job["state"], "pages": job["pages"],
                    "captured": job["captured"], "rejected": job["rejected"], "attempts": job["attempts"],
                    "reason": job["reason"], "cursor": job["cursor"]}
        source = "history-progress:" + job["id"] + ":" + str(job["revision"])
        job["report"] = {"schema_version": "1.0", "source_event_id": source, "instance": instance,
                         "event_type": "audit.history_recovery",
                         "conversation": {"type": job["kind"], "id": job["peer"], "name": None},
                         "occurred_at": iso(time.time()), "metadata": {"history_recovery": progress}}
        self.save(job)


class HistoryRecovery:
    def __init__(self, path, reporter, instance, bots, *, lookback=86400, page_size=50,
                 max_pages=20, interval=1.0, timeout=30.0):
        self.path, self.reporter, self.instance, self.bots = path, reporter, instance, bots
        self.lookback, self.page_size, self.max_pages = lookback, page_size, max_pages
        self.interval, self.timeout = interval, timeout
        self.store = None
        self.connections = {}
        self.tasks = []

    def start(self):
        self.store = RecoveryStore(self.path)
        self.tasks = [asyncio.create_task(self.connections_loop()), asyncio.create_task(self.work_loop())]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.store:
            self.store.db.close()
        self.store = None
        self.tasks = []

    def observe(self, bot_id, raw):
        if not self.store or raw.get("post_type") != "message":
            return
        kind = raw.get("message_type")
        peer = raw.get("group_id") if kind == "group" else raw.get("user_id")
        if peer is not None and str(peer) != str(bot_id):
            try:
                self.store.remember(str(bot_id), kind, str(peer))
            except Exception:
                logging.getLogger(__name__).exception("history recovery peer checkpoint failed")

    async def connections_loop(self):
        while True:
            try:
                current = self.bots()
                now = time.time()
                for bot_id, bot in current.items():
                    bot_id = str(bot_id)
                    if self.connections.get(bot_id) is not bot:
                        prior = self.store.db.execute("SELECT at FROM online WHERE bot=?", (bot_id,)).fetchone()
                        start = prior["at"] - 30 if prior else now - min(3600, self.lookback)
                        reason = "bootstrap_window" if prior is None else None
                        if start < now - self.lookback:
                            self.store.create(bot_id, "system", bot_id, start, now - self.lookback,
                                              "lookback_limit")
                            start = now - self.lookback
                        self.store.create(bot_id, "system", bot_id, start, now, reason)
                        self.connections[bot_id] = bot
                    self.store.db.execute("INSERT OR REPLACE INTO online VALUES (?,?)", (bot_id, now))
                self.connections = {k: v for k, v in self.connections.items() if k in current}
            except Exception:
                # The next iteration retries without advancing job cursors.
                logging.getLogger(__name__).exception("history recovery connection checkpoint failed")
            await asyncio.sleep(5)

    async def api(self, bot, api, **params):
        await asyncio.sleep(self.interval)
        return await asyncio.wait_for(bot.call_api(api, **params), self.timeout)

    def report(self, job):
        payload = job.get("report")
        if payload:
            if not self.reporter.enqueue(ReportItem("/v1/events", payload,
                                                    key(payload["instance"]["instance_id"] + ":" + payload["source_event_id"]))):
                return False
            job["report"] = None
            self.store.save(job)
        return True

    async def discover(self, bot, job):
        if job["reason"] == "lookback_limit":
            job["state"] = "partial"
            return
        failures = []
        for api, kind, field in (("get_group_list", "group", "group_id"),
                                 ("get_friend_list", "private", "user_id")):
            try:
                result = await self.api(bot, api)
                if not isinstance(result, list):
                    raise ValueError("invalid_directory")
            except Exception:
                failures.append(api)
                continue
            for item in result:
                if isinstance(item, dict):
                    self.store.remember(job["bot"], kind, str(item.get(field, "")))
        try:
            result = await self.api(bot, "get_recent_contact", count=100)
            if not isinstance(result, list):
                raise ValueError("invalid_recent_contacts")
        except Exception:
            failures.append("get_recent_contact")
            result = []
        for item in result:
            if isinstance(item, dict):
                kind = {1: "private", 2: "group"}.get(item.get("chatType"))
                self.store.remember(job["bot"], kind, str(item.get("peerUin", "")))
        for row in self.store.db.execute("SELECT kind,peer FROM peers WHERE bot=?", (job["bot"],)).fetchall():
            self.store.create(job["bot"], row["kind"], row["peer"], job["start"], job["end"], job["reason"],
                              str(uuid5(NAMESPACE_URL, job["id"] + ":" + row["kind"] + ":" + row["peer"])))
        if failures:
            job["reason"] = "discovery_unavailable:" + ",".join(failures)
            raise RuntimeError("discovery_incomplete")
        job["state"], job["reason"] = "partial", "discovery_scope_bounded"

    async def page(self, bot, job):
        params = {"count": self.page_size, "reverseOrder": True, "parse_mult_msg": False,
                  "disable_get_url": True, "message_seq": job["cursor"] or "0",
                  "group_id" if job["kind"] == "group" else "user_id": job["peer"]}
        result = await self.api(bot, "get_group_msg_history" if job["kind"] == "group"
                                else "get_friend_msg_history", **params)
        rows = result.get("messages") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) > self.page_size:
            raise ValueError("invalid_history_page")
        if not rows:
            job["state"], job["reason"] = "partial", "history_exhausted"
            return
        times = [r.get("time") for r in rows if isinstance(r, dict)]
        if len(times) != len(rows) or any(type(t) not in {int, float} or not math.isfinite(t) or t <= 0 for t in times):
            raise ValueError("missing_page_time")
        oldest = min(times)
        oldest_row = rows[times.index(oldest)]
        cursor = str(oldest_row.get("message_id", ""))
        if not cursor or cursor == "0" or cursor in job["seen"]:
            job["state"], job["reason"] = "partial", "pagination_stalled"
            return
        if job["oldest"] is not None and oldest > job["oldest"]:
            job["state"], job["reason"] = "partial", "pagination_direction"
            return
        captured, rejected = 0, 0
        for raw in rows:
            if not job["start"] <= raw["time"] <= job["end"]:
                continue
            try:
                payload = history_message(raw, self.instance(job["bot"]), job["kind"], job["peer"])
            except (ValueError, TypeError, OverflowError):
                rejected += 1
                continue
            if not self.reporter.enqueue(ReportItem("/v1/events", payload,
                                                    key(payload["instance"]["instance_id"] + ":" + payload["source_event_id"]))):
                raise RuntimeError("spool_unavailable")
            captured += 1
        job["captured"] += captured
        job["rejected"] += rejected
        job["pages"] += 1
        job["cursor"], job["oldest"] = cursor, oldest
        job["seen"].append(cursor)
        if oldest <= job["start"]:
            job["state"] = "partial" if job["reason"] or job["rejected"] else "scanned"
            if job["rejected"]:
                job["reason"] = "unverifiable_messages"
        elif job["pages"] >= self.max_pages:
            job["state"], job["reason"] = "partial", "page_limit"
        else:
            job["state"] = "running"

    async def work_loop(self):
        while True:
            try:
                for job in self.store.jobs(active_only=True):
                    if not self.report(job):
                        break
                    if job["state"] not in {"pending", "running"} or job["retry_at"] > time.time():
                        continue
                    bot = self.bots().get(job["bot"])
                    if bot is None:
                        continue
                    try:
                        if job["kind"] == "system":
                            await self.discover(bot, job)
                        else:
                            await self.page(bot, job)
                    except Exception as exc:
                        job["attempts"] += 1
                        job["retry_at"] = time.time() + 60
                        if job["attempts"] >= 3:
                            job["state"], job["reason"] = "partial", "api_or_capture_" + type(exc).__name__
                    self.store.checkpoint(job, self.instance(job["bot"]))
                    if not self.report(job):
                        break
            except Exception:
                logging.getLogger(__name__).exception("history recovery worker iteration failed")
            await asyncio.sleep(5)
