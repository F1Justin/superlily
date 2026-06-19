import asyncio
import contextvars
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any
from uuid import uuid4

from nonebot import get_bots, get_driver, get_plugin_config
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.log import logger
from nonebot.message import event_postprocessor, event_preprocessor
from pydantic import BaseModel, BeforeValidator, Field, SecretStr

from .payloads import (
    conversation_from_api,
    conversation_from_event,
    message_segments,
    model_dict,
    source_event_id,
    stable_key,
    utc_iso,
)
from .reporter import BackgroundReporter, ReportItem


class Config(BaseModel):
    lily_core_url: str = "http://127.0.0.1:8765"
    lily_core_token: SecretStr = SecretStr("")
    lily_core_instance_id: str = "lily-command"
    lily_core_bot_id: Annotated[str, BeforeValidator(str)] = ""
    lily_core_role: str = "command"
    lily_core_heartbeat_seconds: int = Field(default=30, ge=5, le=300)
    lily_core_queue_size: int = Field(default=1000, ge=10, le=10000)
    lily_core_timeout_seconds: float = Field(default=0.5, ge=0.05, le=5)
    lily_core_include_raw: bool = False


plugin_config = get_plugin_config(Config)
reporter = BackgroundReporter(
    plugin_config.lily_core_url,
    plugin_config.lily_core_token.get_secret_value(),
    plugin_config.lily_core_queue_size,
    plugin_config.lily_core_timeout_seconds,
)
driver = get_driver()
current_source_event: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "lily_core_source_event", default=None
)
api_started: dict[int, float] = {}
heartbeat_task: asyncio.Task | None = None

try:
    nonebot_version = version("nonebot2")
except PackageNotFoundError:
    nonebot_version = None


def instance(bot_id: str | None = None) -> dict[str, Any]:
    return {
        "instance_id": plugin_config.lily_core_instance_id,
        "platform": "qq",
        "adapter": "onebot_v11",
        "bot_id": str(bot_id or plugin_config.lily_core_bot_id or "unknown"),
        "role": plugin_config.lily_core_role,
        "display_name": "Lily Command",
        "version": nonebot_version,
    }


async def _observe_event(bot: OneBotBot, event: OneBotEvent) -> None:
    raw = model_dict(event)
    if raw.get("post_type") == "message_sent":
        return
    conversation = conversation_from_event(event)
    event_id = source_event_id(event, conversation, raw)
    current_source_event.set(event_id)
    message = None
    if hasattr(event, "get_message"):
        try:
            text, segments, attachments = message_segments(event.get_message())
            message = {
                "id": str(getattr(event, "message_id", "")) or None,
                "text": text,
                "segments": segments,
                "attachments": attachments,
            }
        except Exception:
            logger.opt(exception=True).debug("Lily Core could not normalize an event message")
    sender_obj = getattr(event, "sender", None)
    sender_id = getattr(event, "user_id", None)
    sender = None
    if sender_id is not None:
        sender = {
            "id": str(sender_id),
            "name": getattr(sender_obj, "card", None) or getattr(sender_obj, "nickname", None),
            "roles": [str(getattr(sender_obj, "role", "member"))],
        }
    payload = {
        "schema_version": "1.0",
        "source_event_id": event_id,
        "instance": instance(bot.self_id),
        "event_type": event.get_event_name() if hasattr(event, "get_event_name") else "event",
        "conversation": conversation,
        "sender": sender,
        "message": message,
        "occurred_at": utc_iso(getattr(event, "time", None)),
        "raw": raw if plugin_config.lily_core_include_raw else None,
        "metadata": {"to_me": bool(getattr(event, "to_me", False))},
    }
    reporter.enqueue(
        ReportItem("/v1/events", payload, stable_key(plugin_config.lily_core_instance_id, event_id))
    )


@event_preprocessor
async def observe_event(bot: OneBotBot, event: OneBotEvent) -> None:
    try:
        await _observe_event(bot, event)
    except Exception:
        logger.opt(exception=True).warning("Lily Core event observation failed open")


@event_postprocessor
async def clear_event_context() -> None:
    current_source_event.set(None)


@OneBotBot.on_calling_api
async def observe_api_start(_: OneBotBot, api: str, data: dict[str, Any]) -> None:
    if api.startswith("send_"):
        api_started[id(data)] = time.monotonic()


@OneBotBot.on_called_api
async def observe_api_result(
    bot: OneBotBot,
    exception: Exception | None,
    api: str,
    data: dict[str, Any],
    result: Any,
) -> None:
    if not api.startswith("send_"):
        return
    started = api_started.pop(id(data), None)
    latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    result_dict = result if isinstance(result, dict) else model_dict(result)
    platform_message_id = result_dict.get("message_id") if result_dict else None
    source_response = (
        f"qq:{bot.self_id}:message:{platform_message_id}"
        if platform_message_id is not None
        else f"qq:{bot.self_id}:attempt:{uuid4()}"
    )
    text, segments, attachments = message_segments(data.get("message"))
    conversation = conversation_from_api(data)
    payload = {
        "schema_version": "1.0",
        "source_response_id": source_response,
        "instance": instance(bot.self_id),
        "trigger_source_event_id": current_source_event.get(),
        "response_type": api,
        "conversation": conversation,
        "platform_message_id": str(platform_message_id) if platform_message_id is not None else None,
        "reply_to_platform_message_id": str(data.get("message_id")) if data.get("message_id") else None,
        "text": text,
        "segments": segments,
        "attachments": attachments,
        "success": exception is None,
        "error": str(exception) if exception else None,
        "latency_ms": latency_ms,
        "occurred_at": utc_iso(),
        "raw": {"api": api, "result": result_dict} if plugin_config.lily_core_include_raw else None,
        "metadata": {},
    }
    reporter.enqueue(
        ReportItem(
            "/v1/responses",
            payload,
            stable_key(plugin_config.lily_core_instance_id, source_response),
        )
    )


async def heartbeat_loop() -> None:
    while True:
        bots = list(get_bots().values())
        bot_id = str(bots[0].self_id) if bots else str(plugin_config.lily_core_bot_id or "unknown")
        payload = {
            "schema_version": "1.0",
            "instance": instance(bot_id),
            "process_status": "running",
            "connection_status": "connected" if bots else "disconnected",
            "occurred_at": utc_iso(),
            "metadata": {"connected_bots": len(bots), "queue_depth": reporter.queue.qsize(), "dropped": reporter.dropped},
        }
        reporter.enqueue(ReportItem("/v1/heartbeats", payload))
        await asyncio.sleep(plugin_config.lily_core_heartbeat_seconds)


@driver.on_startup
async def start_bridge() -> None:
    global heartbeat_task
    if not reporter.enabled:
        logger.warning("Lily Core bridge disabled because LILY_CORE_TOKEN is empty")
        return
    await reporter.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="lily-core-heartbeat")
    logger.info("Lily Core bridge started in fail-open mode")


@driver.on_shutdown
async def stop_bridge() -> None:
    global heartbeat_task
    if heartbeat_task:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        heartbeat_task = None
    await reporter.stop()
