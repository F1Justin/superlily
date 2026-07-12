import asyncio
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any
from uuid import uuid4

from nonebot import get_bots, get_driver, get_plugin_config
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.exception import MockApiException
from nonebot.log import logger
from nonebot.matcher import current_event
from nonebot.message import event_postprocessor, event_preprocessor
from pydantic import BaseModel, BeforeValidator, Field, SecretStr

from .payloads import (
    conversation_from_api,
    conversation_from_event,
    event_message,
    message_references,
    message_segments,
    model_dict,
    native_message_identity,
    source_event_id,
    stable_key,
    utc_iso,
)
from .reporter import BackgroundReporter, ReportItem
from .runtime_registry import collect_runtime_registry

BRIDGE_VERSION = "0.2.0"
ONEBOT_QQ_CAPABILITIES = {
    "profile": "onebot_v11.qq.v1",
    "supported": ["mention", "reply", "send_image", "send_text"],
    "limits": {},
}


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
    lily_core_claim_enabled: bool = False


plugin_config = get_plugin_config(Config)
reporter = BackgroundReporter(
    plugin_config.lily_core_url,
    plugin_config.lily_core_token.get_secret_value(),
    plugin_config.lily_core_queue_size,
    plugin_config.lily_core_timeout_seconds,
)
driver = get_driver()
event_contexts: dict[int, dict[str, Any]] = {}
api_started: dict[int, float] = {}
blocked_api_calls: set[int] = set()
heartbeat_task: asyncio.Task | None = None
last_runtime_snapshot_hash: str | None = None
last_runtime_snapshot_sent_at = 0.0

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


async def _observe_event(bot: OneBotBot, event: OneBotEvent) -> tuple[dict[str, Any], str] | None:
    raw = model_dict(event)
    if raw.get("post_type") == "message_sent":
        return None
    conversation = conversation_from_event(event)
    event_id = source_event_id(event, conversation, raw)
    message = None
    if hasattr(event, "get_message"):
        try:
            text, segments, attachments = message_segments(event_message(event))
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
    metadata: dict[str, Any] = {"to_me": bool(getattr(event, "to_me", False))}
    native_identity = native_message_identity(raw, event) if raw.get("post_type") == "message" else {}
    if native_identity:
        metadata["native_identity"] = native_identity
    payload = {
        "schema_version": "1.0",
        "source_event_id": event_id,
        "instance": instance(bot.self_id),
        "event_type": event.get_event_name() if hasattr(event, "get_event_name") else "event",
        "conversation": conversation,
        "sender": sender,
        "message": message,
        "references": message_references(message["segments"], conversation) if message else [],
        "occurred_at": utc_iso(getattr(event, "time", None)),
        "raw": raw if plugin_config.lily_core_include_raw else None,
        "metadata": metadata,
    }
    idempotency_key = stable_key(plugin_config.lily_core_instance_id, event_id)
    reporter.enqueue(ReportItem("/v1/events", payload, idempotency_key))
    return payload, idempotency_key


@event_preprocessor
async def observe_event(bot: OneBotBot, event: OneBotEvent) -> None:
    claim: dict[str, Any] | None = None
    observed: tuple[dict[str, Any], str] | None = None
    try:
        observed = await _observe_event(bot, event)
        if observed is not None and plugin_config.lily_core_claim_enabled:
            payload, idempotency_key = observed
            if payload["event_type"].split(".", 1)[0] == "message":
                claim = await reporter.request_claim(payload, idempotency_key)
    except Exception:
        logger.opt(exception=True).warning("Lily Core event observation failed open")
    if observed is None:
        return
    payload, _ = observed
    denied = bool(claim and claim.get("enforced") is True and claim.get("action") == "deny")
    reason = str(claim.get("reason") or "assigned_to_another_instance") if denied and claim else None
    event_contexts[id(event)] = {
        "source_event_id": payload["source_event_id"],
        "claim_denied": denied,
        "claim_reason": reason,
    }
    if denied:
        logger.info(
            f"Lily Core will suppress sends for event {claim.get('source_event_id')} "
            f"({claim.get('reason')})"
        )


@event_postprocessor
async def clear_event_context(event: OneBotEvent) -> None:
    event_contexts.pop(id(event), None)


def _active_event_context() -> dict[str, Any]:
    event = current_event.get(None)
    return event_contexts.get(id(event), {}) if event is not None else {}


@OneBotBot.on_calling_api
async def observe_api_start(_: OneBotBot, api: str, data: dict[str, Any]) -> None:
    if api.startswith("send_"):
        api_started[id(data)] = time.monotonic()
        if _active_event_context().get("claim_denied") is True:
            blocked_api_calls.add(id(data))
            raise MockApiException({"message_id": -1})


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
    blocked = id(data) in blocked_api_calls
    blocked_api_calls.discard(id(data))
    latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    result_dict = result if isinstance(result, dict) else model_dict(result)
    platform_message_id = None if blocked else result_dict.get("message_id") if result_dict else None
    source_response = (
        f"qq:{bot.self_id}:message:{platform_message_id}"
        if platform_message_id is not None
        else f"qq:{bot.self_id}:attempt:{uuid4()}"
    )
    text, segments, attachments = message_segments(data.get("message"))
    reply_to_platform_message_id = None
    for segment in segments:
        if segment.get("type") != "reply":
            continue
        segment_data = segment.get("data", {}) or {}
        reply_to_platform_message_id = str(
            segment_data.get("id") or segment_data.get("message_id") or ""
        ) or None
        break
    conversation = conversation_from_api(data)
    event_context = _active_event_context()
    payload = {
        "schema_version": "1.0",
        "source_response_id": source_response,
        "instance": instance(bot.self_id),
        "trigger_source_event_id": event_context.get("source_event_id"),
        "response_type": api,
        "conversation": conversation,
        "platform_message_id": str(platform_message_id) if platform_message_id is not None else None,
        "reply_to_platform_message_id": reply_to_platform_message_id,
        "text": text,
        "segments": segments,
        "attachments": attachments,
        "success": exception is None and not blocked,
        "error": "blocked_by_core_claim" if blocked else str(exception) if exception else None,
        "latency_ms": latency_ms,
        "occurred_at": utc_iso(),
        "raw": {"api": api, "result": result_dict} if plugin_config.lily_core_include_raw else None,
        "metadata": {
            "claim_send_suppressed": blocked,
            "claim_reason": event_context.get("claim_reason") if blocked else None,
        },
    }
    reporter.enqueue(
        ReportItem(
            "/v1/responses",
            payload,
            stable_key(plugin_config.lily_core_instance_id, source_response),
        )
    )


async def heartbeat_loop() -> None:
    global last_runtime_snapshot_hash, last_runtime_snapshot_sent_at
    while True:
        bots = list(get_bots().values())
        bot_id = str(bots[0].self_id) if bots else str(plugin_config.lily_core_bot_id or "unknown")
        payload = {
            "schema_version": "1.0",
            "instance": instance(bot_id),
            "process_status": "running",
            "connection_status": "connected" if bots else "disconnected",
            "occurred_at": utc_iso(),
            "capabilities": ONEBOT_QQ_CAPABILITIES,
            "metadata": {
                "connected_bots": len(bots),
                "queue_depth": reporter.queue.qsize(),
                "dropped": reporter.dropped,
                "claim_enabled": plugin_config.lily_core_claim_enabled,
                "claim_failures": reporter.claim_failures,
                "bridge_version": BRIDGE_VERSION,
            },
        }
        reporter.enqueue(ReportItem("/v1/heartbeats", payload))
        try:
            runtime_snapshot = collect_runtime_registry()
            now = time.monotonic()
            if (
                runtime_snapshot["snapshot_hash"] != last_runtime_snapshot_hash
                or now - last_runtime_snapshot_sent_at >= 300
            ):
                snapshot_payload = {
                    "schema_version": "1.0",
                    "instance": instance(bot_id),
                    "snapshot_hash": runtime_snapshot["snapshot_hash"],
                    "observed_at": utc_iso(),
                    "plugins": runtime_snapshot["plugins"],
                    "candidates": runtime_snapshot["candidates"],
                }
                accepted = reporter.enqueue(
                    ReportItem(
                        "/v1/command-registry/snapshots",
                        snapshot_payload,
                        stable_key(
                            plugin_config.lily_core_instance_id,
                            f"command-registry:{runtime_snapshot['snapshot_hash']}",
                        ),
                    )
                )
                if accepted:
                    last_runtime_snapshot_hash = runtime_snapshot["snapshot_hash"]
                    last_runtime_snapshot_sent_at = now
        except Exception:
            logger.opt(exception=True).warning("Lily Core runtime command snapshot failed open")
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
