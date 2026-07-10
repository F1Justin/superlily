import asyncio
import hashlib
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from functools import wraps
from typing import Any
from uuid import uuid4

import nonebot.message as nonebot_message
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.message import event_preprocessor
from pydantic import Field

from nekro_agent.api.core import logger
from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.signal import MsgSignal

from .identity import NativeIdentityCache, conversation, native_identity_cache_key
from .payloads import content_parts, message_references, native_message_identity, ref_msg_id_from_ext_data
from .reporter import BackgroundReporter, ReportItem

plugin = NekroPlugin(
    name="Lily Core Bridge",
    module_name="core_bridge",
    description="Fail-open event, response, and heartbeat reporting to Lily Core",
    version="0.1.0",
    author="Superlily",
    url="",
    support_adapter=["onebot_v11"],
)


@plugin.mount_config()
class BridgeConfig(ConfigBase):
    CORE_URL: str = Field(default="http://lily-core:8000", title="Lily Core URL")
    CORE_TOKEN: str = Field(default="", title="Instance bearer token")
    INSTANCE_ID: str = Field(default="nekro-agent", title="Core instance ID")
    BOT_ID: str = Field(default="", title="QQ bot ID")
    HEARTBEAT_SECONDS: int = Field(default=30, ge=5, le=300, title="Heartbeat interval")
    QUEUE_SIZE: int = Field(default=1000, ge=10, le=10000, title="In-memory queue size")
    TIMEOUT_SECONDS: float = Field(default=0.5, ge=0.05, le=5, title="HTTP timeout")


config: BridgeConfig = plugin.get_config(BridgeConfig)
reporter = BackgroundReporter(config.CORE_URL, config.CORE_TOKEN, config.QUEUE_SIZE, config.TIMEOUT_SECONDS)
heartbeat_task: asyncio.Task | None = None

try:
    nekro_version = version("nekro-agent")
except PackageNotFoundError:
    nekro_version = "2.2.1"


def utc_iso(timestamp: int | float | None = None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def stable_key(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()


def instance(bot_id: str | None = None) -> dict[str, Any]:
    return {
        "instance_id": config.INSTANCE_ID,
        "platform": "qq",
        "adapter": "onebot_v11",
        "bot_id": str(bot_id or config.BOT_ID or "unknown"),
        "role": "talk",
        "display_name": "Nekro Agent",
        "version": nekro_version,
    }


async def _observe_user_message(message: ChatMessage) -> None:
    conv = conversation(message.chat_key, message.chat_type)
    source_id = f"qq:{conv['type']}:{conv['id']}:message:{message.message_id}"
    segments, attachments = content_parts(message.content_data)
    ref_msg_id = ref_msg_id_from_ext_data(message.ext_data)
    native_identity = _take_native_identity(conv, message.message_id)
    if native_identity is None:
        fallback_identity = {
            "message_id": message.message_id,
            "user_id": message.platform_userid or message.sender_id,
            "message_type": conv["type"],
        }
        if conv["type"] == "group":
            fallback_identity["group_id"] = conv["id"]
        native_identity = native_message_identity(message.ext_data, fallback_identity)
    metadata: dict[str, Any] = {"is_tome": bool(message.is_tome), "chat_key": message.chat_key}
    if native_identity:
        metadata["native_identity"] = native_identity
    payload = {
        "schema_version": "1.0",
        "source_event_id": source_id,
        "instance": instance(),
        "event_type": "message",
        "conversation": conv,
        "sender": {
            "id": str(message.platform_userid or message.sender_id),
            "name": message.sender_nickname or message.sender_name,
            "roles": [],
        },
        "message": {
            "id": str(message.message_id),
            "text": message.content_text,
            "segments": segments,
            "attachments": attachments,
        },
        "references": message_references(segments, conv, ref_msg_id),
        "occurred_at": utc_iso(message.send_timestamp),
        "raw": None,
        "metadata": metadata,
    }
    reporter.enqueue(ReportItem("/v1/events", payload, stable_key(config.INSTANCE_ID, source_id)))


@plugin.mount_on_user_message()
async def observe_user_message(_: AgentCtx, message: ChatMessage) -> MsgSignal:
    try:
        await _observe_user_message(message)
    except Exception:
        logger.exception("Lily Core user-message observation failed open")
    return MsgSignal.CONTINUE


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    if hasattr(event, "dict"):
        return event.dict()
    return {}


_NATIVE_IDENTITY_CACHE_ATTR = "_superlily_native_identity_cache_v1"
_NATIVE_IDENTITY_HOOK_ATTR = "_superlily_native_identity_hook_v1"


def _native_identity_cache() -> NativeIdentityCache:
    cache = getattr(nonebot_message, _NATIVE_IDENTITY_CACHE_ATTR, None)
    if cache is None:
        cache = NativeIdentityCache()
        setattr(nonebot_message, _NATIVE_IDENTITY_CACHE_ATTR, cache)
    return cache


def _onebot_conversation(raw: dict[str, Any]) -> dict[str, str]:
    if raw.get("group_id") is not None:
        return {"id": str(raw["group_id"]), "type": "group"}
    return {"id": str(raw.get("user_id") or raw.get("target_id") or "unknown"), "type": "private"}


def _take_native_identity(conv: dict[str, Any], message_id: Any) -> dict[str, str] | None:
    return _native_identity_cache().pop(native_identity_cache_key(conv, message_id))


if not getattr(nonebot_message, _NATIVE_IDENTITY_HOOK_ATTR, False):

    @event_preprocessor
    async def capture_native_identity(bot: OneBotBot, event: OneBotEvent) -> None:
        try:
            raw = _event_dict(event)
            if raw.get("post_type") != "message" or raw.get("message_id") is None:
                return
            conv = _onebot_conversation(raw)
            identity = native_message_identity(raw, event)
            _native_identity_cache().put(
                native_identity_cache_key(conv, raw["message_id"]),
                identity,
            )
        except Exception:
            logger.exception("Lily Core native identity capture failed open")

    setattr(nonebot_message, _NATIVE_IDENTITY_HOOK_ATTR, True)


def _onebot_message_parts(message: Any) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for segment in message or []:
        if isinstance(segment, dict):
            segment_type = str(segment.get("type", "unknown"))
            data = dict(segment.get("data", {}) or {})
        else:
            segment_type = str(getattr(segment, "type", "unknown"))
            data = dict(getattr(segment, "data", {}) or {})
        data = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        segments.append({"type": segment_type, "data": data})
        if segment_type == "text":
            texts.append(str(data.get("text", "")))
        if segment_type in {"image", "file", "record", "video"}:
            file_value = data.get("file")
            platform_id = None
            if file_value and not str(file_value).lower().startswith(
                ("http://", "https://", "file://", "base64://", "data:")
            ):
                platform_id = str(file_value)[:512]
            attachments.append(
                {
                    "type": segment_type,
                    "name": data.get("name") or data.get("file_name"),
                    "platform_id": platform_id,
                    "size_bytes": data.get("file_size") if isinstance(data.get("file_size"), int) else None,
                }
            )
    return "".join(texts) or None, segments, attachments


def _fail_open_event(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception:
            logger.exception("Lily Core message-sent observation failed open")
            return None

    return wrapper


if not getattr(nonebot_message, "_superlily_message_sent_hook", False):

    @event_preprocessor
    @_fail_open_event
    async def observe_message_sent(bot: OneBotBot, event: OneBotEvent) -> None:
        raw = _event_dict(event)
        if raw.get("post_type") != "message_sent":
            return
        if raw.get("group_id") is not None:
            conv = {"id": str(raw["group_id"]), "type": "group", "name": raw.get("group_name")}
        else:
            conv = {"id": str(raw.get("target_id") or raw.get("user_id") or "unknown"), "type": "private", "name": None}
        text, segments, attachments = _onebot_message_parts(getattr(event, "message", None))
        message_id = raw.get("message_id")
        source_response = f"qq:{bot.self_id}:message:{message_id}"
        reply_id = None
        for segment in segments:
            if segment.get("type") == "reply":
                reply_id = str(segment.get("data", {}).get("id") or "") or None
                break
        payload = {
            "schema_version": "1.0",
            "source_response_id": source_response,
            "instance": instance(bot.self_id),
            "response_type": "message_sent",
            "conversation": conv,
            "platform_message_id": str(message_id) if message_id is not None else None,
            "reply_to_platform_message_id": reply_id,
            "text": text,
            "segments": segments,
            "attachments": attachments,
            "success": True,
            "occurred_at": utc_iso(raw.get("time")),
            "raw": None,
            "metadata": {},
        }
        reporter.enqueue(
            ReportItem("/v1/responses", payload, stable_key(config.INSTANCE_ID, source_response))
        )

    nonebot_message._superlily_message_sent_hook = True


if not getattr(OneBotBot, "_superlily_failed_send_hook", False):

    @OneBotBot.on_called_api
    async def observe_failed_send(
        bot: OneBotBot,
        exception: Exception | None,
        api: str,
        data: dict[str, Any],
        result: Any,
    ) -> None:
        if exception is None or not api.startswith("send_"):
            return
        if data.get("group_id") is not None:
            conv = {"id": str(data["group_id"]), "type": "group", "name": None}
        else:
            conv = {"id": str(data.get("user_id") or "unknown"), "type": "private", "name": None}
        source_response = f"qq:{bot.self_id}:failed-attempt:{uuid4()}"
        text, segments, attachments = _onebot_message_parts(data.get("message"))
        payload = {
            "schema_version": "1.0",
            "source_response_id": source_response,
            "instance": instance(bot.self_id),
            "response_type": api,
            "conversation": conv,
            "text": text,
            "segments": segments,
            "attachments": attachments,
            "success": False,
            "error": str(exception),
            "occurred_at": utc_iso(),
            "raw": None,
            "metadata": {},
        }
        reporter.enqueue(
            ReportItem("/v1/responses", payload, stable_key(config.INSTANCE_ID, source_response))
        )

    OneBotBot._superlily_failed_send_hook = True


async def heartbeat_loop() -> None:
    while True:
        bots = list(get_bots().values())
        bot_id = str(bots[0].self_id) if bots else config.BOT_ID or "unknown"
        reporter.enqueue(
            ReportItem(
                "/v1/heartbeats",
                {
                    "schema_version": "1.0",
                    "instance": instance(bot_id),
                    "process_status": "running",
                    "connection_status": "connected" if bots else "disconnected",
                    "occurred_at": utc_iso(),
                    "metadata": {
                        "connected_bots": len(bots),
                        "queue_depth": reporter.queue.qsize(),
                        "dropped": reporter.dropped,
                    },
                },
            )
        )
        await asyncio.sleep(config.HEARTBEAT_SECONDS)


@plugin.mount_init_method()
async def init_bridge() -> None:
    global heartbeat_task
    if not reporter.enabled:
        logger.warning("Lily Core bridge disabled because CORE_TOKEN is empty")
        return
    await reporter.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="nekro-lily-core-heartbeat")
    logger.info("Lily Core bridge started in fail-open mode")


@plugin.mount_cleanup_method()
async def cleanup_bridge() -> None:
    global heartbeat_task
    if heartbeat_task:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        heartbeat_task = None
    await reporter.stop()

__all__ = ["plugin"]
