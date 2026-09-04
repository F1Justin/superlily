import asyncio
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated, Any
from uuid import uuid4

import httpx
from nonebot import get_bots, get_driver, get_plugin_config, on_command
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.exception import MockApiException
from nonebot.log import logger
from nonebot.matcher import current_event
from nonebot.message import event_postprocessor, event_preprocessor
from nonebot.params import CommandArg
from pydantic import BaseModel, BeforeValidator, Field, SecretStr

from .command_rendering import (
    Phase4CommandClient,
    Phase4CommandFallback,
    Phase4CommandSuppressed,
    PreparedDelivery,
    command_idempotency_key,
)
from .directory_snapshots import (
    await_qq_api,
    friend_directory_snapshot,
    group_directory_snapshot,
)
from .platform_actions import platform_action_event_payload
from .platform_api_audit import completed_api_call, is_audited_side_effect, started_api_call
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

BRIDGE_VERSION = "0.9.0"
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
    lily_core_group_inventory_seconds: int = Field(default=21_600, ge=300, le=86_400)
    lily_core_directory_snapshot_enabled: bool = False
    lily_core_directory_snapshot_seconds: int = Field(default=86_400, ge=3_600, le=604_800)
    lily_core_directory_api_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    lily_core_queue_size: int = Field(default=1000, ge=10, le=10000)
    lily_core_timeout_seconds: float = Field(default=0.5, ge=0.05, le=5)
    lily_core_claim_timeout_seconds: float = Field(default=10.0, ge=0.05, le=30)
    lily_core_report_timeout_seconds: float = Field(default=10.0, ge=0.05, le=30)
    lily_core_report_attempts: int = Field(default=3, ge=1, le=10)
    lily_core_report_retry_backoff_seconds: float = Field(default=0.1, ge=0, le=5)
    lily_core_claim_attempts: int = Field(default=2, ge=1, le=5)
    lily_core_claim_retry_backoff_seconds: float = Field(default=0.1, ge=0, le=5)
    lily_core_spool_path: str = "/home/justin/lily/data/superlily-core/ingress-spool.sqlite3"
    lily_core_spool_quota_bytes: int = Field(default=268_435_456, ge=1_048_576, le=4_294_967_296)
    lily_core_spool_retention_seconds: int = Field(default=86_400, ge=0, le=604_800)
    lily_core_spool_max_record_bytes: int = Field(default=1_048_576, ge=65_536, le=8_388_608)
    lily_core_include_raw: bool = False
    lily_core_claim_enabled: bool = False
    lily_core_phase4_commands_enabled: bool = False
    lily_core_phase4_command_canary_groups: str = ""
    lily_core_phase4_status_enabled: bool = True
    lily_core_phase4_wolfram_enabled: bool = True
    lily_core_phase4_latex_enabled: bool = True
    lily_core_phase4_help_enabled: bool = True
    lily_core_phase4_command_timeout_seconds: float = Field(
        default=10.0,
        ge=1,
        le=30,
    )


plugin_config = get_plugin_config(Config)
reporter = BackgroundReporter(
    plugin_config.lily_core_url,
    plugin_config.lily_core_token.get_secret_value(),
    plugin_config.lily_core_queue_size,
    plugin_config.lily_core_claim_timeout_seconds,
    plugin_config.lily_core_report_timeout_seconds,
    plugin_config.lily_core_report_attempts,
    plugin_config.lily_core_report_retry_backoff_seconds,
    plugin_config.lily_core_claim_attempts,
    plugin_config.lily_core_claim_retry_backoff_seconds,
    plugin_config.lily_core_spool_path,
    plugin_config.lily_core_spool_quota_bytes,
    plugin_config.lily_core_spool_retention_seconds,
    plugin_config.lily_core_spool_max_record_bytes,
)
phase4_command_client = Phase4CommandClient(
    plugin_config.lily_core_url,
    plugin_config.lily_core_token.get_secret_value(),
    request_timeout_seconds=plugin_config.lily_core_phase4_command_timeout_seconds,
)
driver = get_driver()
event_contexts: dict[int, dict[str, Any]] = {}
api_started: dict[int, float] = {}
api_audit_contexts: dict[int, tuple[float, dict[str, Any]]] = {}
blocked_api_calls: set[int] = set()
heartbeat_task: asyncio.Task | None = None
group_inventory_task: asyncio.Task | None = None
directory_snapshot_task: asyncio.Task | None = None
group_names: dict[str, str] = {}
heartbeat_failures = 0
last_heartbeat_error: str | None = None
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


def _group_name(value: Any) -> str | None:
    item = value if isinstance(value, dict) else model_dict(value)
    name = item.get("group_name") or item.get("name")
    return str(name).strip() if name is not None and str(name).strip() else None


def _sender_profile_text(value: Any) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    normalized = str(value).strip()
    return normalized[:512] if normalized else None


async def _conversation_with_name(
    bot: OneBotBot,
    conversation: dict[str, Any],
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    if conversation.get("type") != "group":
        return conversation
    group_id = str(conversation["id"])
    name = None if refresh else group_names.get(group_id)
    if name is None:
        try:
            try:
                info = await bot.get_group_info(group_id=int(group_id), no_cache=False)
            except TypeError:
                info = await bot.get_group_info(group_id=int(group_id))
            name = _group_name(info)
            if name is not None:
                group_names[group_id] = name
        except Exception:
            logger.opt(exception=True).debug(f"Lily Core could not refresh QQ group name for {group_id}")
    return {**conversation, "name": name}


def _group_name_snapshot(bot: OneBotBot, group_id: str, name: str, observed_at: str) -> ReportItem:
    event_id = f"qq:{bot.self_id}:meta:conversation-name:{group_id}:{stable_key(name, observed_at)}"
    payload = {
        "schema_version": "1.0",
        "source_event_id": event_id,
        "instance": instance(bot.self_id),
        "event_type": "meta.conversation_name_snapshot",
        "conversation": {"id": group_id, "type": "group", "name": name},
        "sender": None,
        "message": None,
        "references": [],
        "occurred_at": observed_at,
        "raw": None,
        "metadata": {"observation_method": "onebot_get_group_list"},
    }
    return ReportItem(
        "/v1/events",
        payload,
        stable_key(plugin_config.lily_core_instance_id, event_id),
    )


async def group_inventory_loop() -> None:
    while True:
        try:
            bots = [bot for bot in get_bots().values() if isinstance(bot, OneBotBot)]
            if not bots:
                await asyncio.sleep(min(30, plugin_config.lily_core_group_inventory_seconds))
                continue
            for bot in bots:
                observed_at = utc_iso()
                try:
                    groups = await bot.get_group_list(no_cache=False)
                except TypeError:
                    groups = await bot.get_group_list()
                for raw_group in groups:
                    item = raw_group if isinstance(raw_group, dict) else model_dict(raw_group)
                    group_id = item.get("group_id") or item.get("id")
                    name = _group_name(item)
                    if group_id is None or name is None:
                        continue
                    canonical_id = str(group_id)
                    group_names[canonical_id] = name
                    reporter.enqueue(_group_name_snapshot(bot, canonical_id, name, observed_at))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Lily Core group-name inventory failed; the loop will continue")
        await asyncio.sleep(plugin_config.lily_core_group_inventory_seconds)


async def directory_snapshot_loop() -> None:
    while True:
        retry_seconds = plugin_config.lily_core_directory_snapshot_seconds
        try:
            bots = [bot for bot in get_bots().values() if isinstance(bot, OneBotBot)]
            if not bots:
                await asyncio.sleep(min(30, plugin_config.lily_core_directory_snapshot_seconds))
                continue
            for bot in bots:
                observed_at = utc_iso()
                try:
                    categories = await await_qq_api(
                        bot.call_api("get_friends_with_category"),
                        timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                    )
                    payload, key = friend_directory_snapshot(
                        instance=instance(bot.self_id),
                        raw_categories=categories if isinstance(categories, list) else [],
                        observed_at=observed_at,
                        source_apis=["get_friends_with_category"],
                    )
                except Exception as category_error:
                    try:
                        friends = await await_qq_api(
                            bot.get_friend_list(),
                            timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                        )
                        payload, key = friend_directory_snapshot(
                            instance=instance(bot.self_id),
                            raw_categories=[{"buddyList": friends}],
                            observed_at=observed_at,
                            source_apis=["get_friends_with_category", "get_friend_list"],
                            capture_status="partial",
                            reason=f"get_friends_with_category:{type(category_error).__name__}",
                        )
                    except Exception:
                        logger.opt(exception=True).warning("Lily Core friend directory snapshot failed")
                    else:
                        reporter.enqueue(ReportItem("/v1/qq-directory/snapshots", payload, key))
                else:
                    reporter.enqueue(ReportItem("/v1/qq-directory/snapshots", payload, key))

                try:
                    try:
                        groups = await await_qq_api(
                            bot.get_group_list(no_cache=False),
                            timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                        )
                    except TypeError:
                        groups = await await_qq_api(
                            bot.get_group_list(),
                            timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                        )
                except Exception:
                    retry_seconds = min(60, retry_seconds)
                    logger.opt(exception=True).warning("Lily Core group directory inventory failed")
                    continue
                if not groups:
                    retry_seconds = min(60, retry_seconds)
                    logger.warning("Lily Core group directory inventory was empty; retrying shortly")
                    continue
                for raw_group in groups:
                    group = raw_group if isinstance(raw_group, dict) else model_dict(raw_group)
                    group_id = group.get("group_id") or group.get("id")
                    if group_id is None:
                        continue
                    source_apis = ["get_group_list", "get_group_info_ex", "get_group_member_list"]
                    reasons: list[str] = []
                    try:
                        extended = await await_qq_api(
                            bot.call_api("get_group_info_ex", group_id=int(group_id)),
                            timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                        )
                        if isinstance(extended, dict):
                            group = {**group, **extended}
                    except Exception as exc:
                        reasons.append(f"get_group_info_ex:{type(exc).__name__}")
                    try:
                        members = await await_qq_api(
                            bot.get_group_member_list(group_id=int(group_id), no_cache=True),
                            timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                        )
                    except TypeError:
                        try:
                            members = await await_qq_api(
                                bot.get_group_member_list(group_id=int(group_id)),
                                timeout_seconds=plugin_config.lily_core_directory_api_timeout_seconds,
                            )
                        except Exception as exc:
                            members = []
                            reasons.append(f"get_group_member_list:{type(exc).__name__}")
                    except Exception as exc:
                        members = []
                        reasons.append(f"get_group_member_list:{type(exc).__name__}")
                    try:
                        payload, key = group_directory_snapshot(
                            instance=instance(bot.self_id),
                            raw_group=group,
                            raw_members=members if isinstance(members, list) else [],
                            observed_at=observed_at,
                            source_apis=source_apis,
                            capture_status="partial" if reasons else "complete",
                            reason="; ".join(reasons) or None,
                        )
                    except Exception:
                        logger.opt(exception=True).warning(
                            f"Lily Core group directory snapshot normalization failed for {group_id}"
                        )
                        continue
                    reporter.enqueue(ReportItem("/v1/qq-directory/snapshots", payload, key))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Lily Core directory snapshot failed; the loop will continue")
        await asyncio.sleep(retry_seconds)


async def _observe_event(bot: OneBotBot, event: OneBotEvent) -> tuple[dict[str, Any], str] | None:
    raw = model_dict(event)
    if raw.get("post_type") == "message_sent":
        return None
    conversation = conversation_from_event(event)
    event_name = event.get_event_name() if hasattr(event, "get_event_name") else "event"
    conversation = await _conversation_with_name(
        bot,
        conversation,
        refresh=event_name == "notice.notify.group_name",
    )
    action_payload = platform_action_event_payload(
        raw,
        conversation,
        instance(bot.self_id),
        event_type=event_name,
        fallback_occurred_at=utc_iso(),
        to_me=bool(getattr(event, "to_me", False)),
    )
    if action_payload is not None:
        event_id = action_payload["source_event_id"]
        return action_payload, stable_key(plugin_config.lily_core_instance_id, event_id)
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
            "account_name": getattr(sender_obj, "nickname", None),
            "display_name": (getattr(sender_obj, "card", None) or getattr(sender_obj, "nickname", None)),
            "name": getattr(sender_obj, "card", None) or getattr(sender_obj, "nickname", None),
            "title": _sender_profile_text(getattr(sender_obj, "title", None)),
            "level": _sender_profile_text(getattr(sender_obj, "level", None)),
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
        "event_type": event_name,
        "conversation": conversation,
        "sender": sender,
        "message": message,
        "references": message_references(message["segments"], conversation) if message else [],
        "occurred_at": utc_iso(getattr(event, "time", None)),
        "raw": raw if plugin_config.lily_core_include_raw else None,
        "metadata": metadata,
    }
    idempotency_key = stable_key(plugin_config.lily_core_instance_id, event_id)
    return payload, idempotency_key


@event_preprocessor
async def observe_event(bot: OneBotBot, event: OneBotEvent) -> None:
    claim: dict[str, Any] | None = None
    observed: tuple[dict[str, Any], str] | None = None
    try:
        observed = await _observe_event(bot, event)
        if observed is not None:
            payload, idempotency_key = observed
            claimable = payload["event_type"].split(".", 1)[0] == "message"
            if plugin_config.lily_core_claim_enabled and claimable:
                claim = await reporter.request_claim(payload, idempotency_key)
                if claim is None:
                    reporter.enqueue(ReportItem("/v1/events", payload, idempotency_key))
            else:
                reporter.enqueue(ReportItem("/v1/events", payload, idempotency_key))
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
        "claim_acknowledged": False,
    }
    if denied:
        claim_id = str(claim.get("claim_id") or "") if claim else ""
        acknowledged = await reporter.acknowledge_claim(claim_id)
        event_contexts[id(event)]["claim_acknowledged"] = acknowledged
        logger.info(
            f"Lily Core will suppress sends for event {claim.get('source_event_id')} "
            f"({claim.get('reason')}; acknowledged={acknowledged})"
        )


@event_postprocessor
async def clear_event_context(event: OneBotEvent) -> None:
    event_contexts.pop(id(event), None)


def _active_event_context() -> dict[str, Any]:
    event = current_event.get(None)
    return event_contexts.get(id(event), {}) if event is not None else {}


@OneBotBot.on_calling_api
async def observe_api_start(bot: OneBotBot, api: str, data: dict[str, Any]) -> None:
    if is_audited_side_effect(api):
        started = time.monotonic()
        try:
            payload, key, context = started_api_call(
                instance=instance(bot.self_id),
                api=api,
                data=data,
                trigger_source_event_id=_active_event_context().get("source_event_id"),
                occurred_at=utc_iso(),
            )
            api_audit_contexts[id(data)] = (started, context)
            reporter.enqueue(ReportItem("/v1/events", payload, key))
        except Exception:
            logger.opt(exception=True).warning("Lily Core could not record platform API call start")
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
    audit_context = api_audit_contexts.pop(id(data), None)
    if audit_context is not None:
        audit_started, context = audit_context
        try:
            payload, key = completed_api_call(
                context,
                exception=exception,
                result=result,
                duration_ms=int((time.monotonic() - audit_started) * 1000),
                occurred_at=utc_iso(),
            )
            reporter.enqueue(ReportItem("/v1/events", payload, key))
        except Exception:
            logger.opt(exception=True).warning("Lily Core could not record platform API call result")
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
        reply_to_platform_message_id = str(segment_data.get("id") or segment_data.get("message_id") or "") or None
        break
    conversation = conversation_from_api(data)
    event_context = _active_event_context()
    error_text = str(exception).lower() if exception is not None else ""
    completion_status = (
        "suppressed"
        if blocked
        else "succeeded"
        if exception is None
        else "ambiguous"
        if "timeout" in error_text or "timed out" in error_text
        else "failed"
    )
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
            "claim_acknowledged": (event_context.get("claim_acknowledged") if blocked else None),
            "trigger_attribution": ("event_context" if event_context.get("source_event_id") else None),
            "completion_status": completion_status,
        },
    }
    reporter.enqueue(
        ReportItem(
            "/v1/responses",
            payload,
            stable_key(plugin_config.lily_core_instance_id, source_response),
        )
    )


phase4_status = on_command("status", priority=11, block=False)
phase4_wolfram = on_command("wf", priority=11, block=False)
phase4_latex = on_command("tex", priority=11, block=False)
phase4_help = on_command("help", priority=11, block=False)
_PHASE4_HELP_FLAGS = frozenset({"-h", "--help", "help", "/help"})
_PHASE4_HELP_COMMANDS = [
    {
        "name": "/status",
        "summary": "查看独立状态 Provider 的运行状态",
        "usage": "/status",
    },
    {
        "name": "/wf",
        "summary": "执行受限 Wolfram 文本计算",
        "usage": "/wf <表达式>",
    },
    {
        "name": "/tex",
        "summary": "在无网络 worker 中渲染 LaTeX 公式",
        "usage": "/tex <公式>",
    },
]


def _phase4_canary_groups() -> frozenset[str]:
    return frozenset(
        value.strip() for value in plugin_config.lily_core_phase4_command_canary_groups.split(",") if value.strip()
    )


def _phase4_command_allowed(event: GroupMessageEvent, enabled: bool) -> bool:
    return bool(
        plugin_config.lily_core_phase4_commands_enabled
        and enabled
        and plugin_config.lily_core_token.get_secret_value()
        and str(event.group_id) in _phase4_canary_groups()
    )


def _phase4_command_context(event: GroupMessageEvent) -> tuple[str, str, list[str]]:
    raw = model_dict(event)
    conv = conversation_from_event(event)
    source_id = source_event_id(event, conv, raw)
    sender = getattr(event, "sender", None)
    role = str(getattr(sender, "role", "member"))
    return (
        f"onebot_v11-group_{event.group_id}",
        source_id,
        [role],
    )


async def _send_phase4_delivery(
    bot: OneBotBot,
    event: GroupMessageEvent,
    matcher,
    prepared: PreparedDelivery,
) -> None:
    # Once an intent exists, the legacy matcher must not also send. Any
    # uncertain OneBot completion remains ambiguous and is never retried.
    matcher.stop_propagation()
    outcome = "ambiguous"
    platform_message_id: str | None = None
    safe_error_code: str | None = "platform_completion_unknown"
    try:
        outbound = (
            MessageSegment.image(prepared.content)
            if prepared.selected_family == "image"
            else MessageSegment.text(str(prepared.content))
        )
        result = await bot.send(event, outbound)
        result_dict = result if isinstance(result, dict) else model_dict(result)
        raw_message_id = result_dict.get("message_id") if result_dict else None
        if raw_message_id is not None:
            platform_message_id = str(raw_message_id)
            outcome = "succeeded"
            safe_error_code = None
    except Exception:
        logger.opt(exception=True).warning("Phase 4 command delivery has unknown platform completion")
    try:
        recorded = await phase4_command_client.complete_delivery(
            instance_id=plugin_config.lily_core_instance_id,
            intent_id=prepared.intent_id,
            outcome=outcome,
            platform_message_id=platform_message_id,
            safe_error_code=safe_error_code,
        )
        if not recorded:
            logger.warning("Phase 4 command delivery completion was not accepted by Core")
    except Exception:
        logger.opt(exception=True).warning("Phase 4 command delivery completion could not be recorded")


async def _prepare_phase4_tool(
    event: GroupMessageEvent,
    *,
    tool_id: str,
    tool_input: dict[str, Any],
) -> PreparedDelivery:
    conversation_key, source_id, roles = _phase4_command_context(event)
    idempotency_key = command_idempotency_key(
        instance_id=plugin_config.lily_core_instance_id,
        source_event_id=source_id,
        command=tool_id,
        arguments=tool_input,
    )
    return await phase4_command_client.prepare_tool_delivery(
        instance_id=plugin_config.lily_core_instance_id,
        conversation_key=conversation_key,
        source_event_id=source_id,
        sender_id=str(event.user_id),
        platform_roles=roles,
        tool_id=tool_id,
        tool_input=tool_input,
        idempotency_key=idempotency_key,
    )


@phase4_status.handle()
async def handle_phase4_status(bot: OneBotBot, event: GroupMessageEvent) -> None:
    if not _phase4_command_allowed(
        event,
        plugin_config.lily_core_phase4_status_enabled,
    ):
        return
    try:
        prepared = await _prepare_phase4_tool(
            event,
            tool_id="status.inspect",
            tool_input={"scope": "provider_runtime"},
        )
    except Phase4CommandSuppressed:
        phase4_status.stop_propagation()
        return
    except (Phase4CommandFallback, httpx.HTTPError):
        return
    except Exception:
        logger.opt(exception=True).warning("Phase 4 status command fell back safely")
        return
    await _send_phase4_delivery(bot, event, phase4_status, prepared)


@phase4_wolfram.handle()
async def handle_phase4_wolfram(
    bot: OneBotBot,
    event: GroupMessageEvent,
    msg: Message = CommandArg(),
) -> None:
    if not _phase4_command_allowed(
        event,
        plugin_config.lily_core_phase4_wolfram_enabled,
    ):
        return
    expression = msg.extract_plain_text().strip()
    if not expression or expression.casefold() in _PHASE4_HELP_FLAGS or any(segment.type == "image" for segment in msg):
        return
    try:
        prepared = await _prepare_phase4_tool(
            event,
            tool_id="wolfram.run",
            tool_input={"expression": expression},
        )
    except Phase4CommandSuppressed:
        phase4_wolfram.stop_propagation()
        return
    except (Phase4CommandFallback, httpx.HTTPError):
        return
    except Exception:
        logger.opt(exception=True).warning("Phase 4 Wolfram command fell back safely")
        return
    await _send_phase4_delivery(bot, event, phase4_wolfram, prepared)


@phase4_latex.handle()
async def handle_phase4_latex(
    bot: OneBotBot,
    event: GroupMessageEvent,
    msg: Message = CommandArg(),
) -> None:
    if not _phase4_command_allowed(
        event,
        plugin_config.lily_core_phase4_latex_enabled,
    ):
        return
    latex = msg.extract_plain_text().strip().strip("$")
    if not latex:
        return
    try:
        prepared = await _prepare_phase4_tool(
            event,
            tool_id="latex.render",
            tool_input={"latex": latex},
        )
    except Phase4CommandSuppressed:
        phase4_latex.stop_propagation()
        return
    except (Phase4CommandFallback, httpx.HTTPError):
        return
    except Exception:
        logger.opt(exception=True).warning("Phase 4 LaTeX command fell back safely")
        return
    await _send_phase4_delivery(bot, event, phase4_latex, prepared)


@phase4_help.handle()
async def handle_phase4_help(bot: OneBotBot, event: GroupMessageEvent) -> None:
    if not _phase4_command_allowed(
        event,
        plugin_config.lily_core_phase4_help_enabled,
    ):
        return
    conversation_key, source_id, _ = _phase4_command_context(event)
    idempotency_key = command_idempotency_key(
        instance_id=plugin_config.lily_core_instance_id,
        source_event_id=source_id,
        command="help.render",
        arguments={"commands": _PHASE4_HELP_COMMANDS},
    )
    try:
        prepared = await phase4_command_client.prepare_help_delivery(
            instance_id=plugin_config.lily_core_instance_id,
            conversation_key=conversation_key,
            source_event_id=source_id,
            commands=_PHASE4_HELP_COMMANDS,
            idempotency_key=idempotency_key,
        )
    except Phase4CommandSuppressed:
        phase4_help.stop_propagation()
        return
    except (Phase4CommandFallback, httpx.HTTPError):
        return
    except Exception:
        logger.opt(exception=True).warning("Phase 4 help command fell back safely")
        return
    await _send_phase4_delivery(bot, event, phase4_help, prepared)


async def heartbeat_loop() -> None:
    global heartbeat_failures, last_heartbeat_error
    global last_runtime_snapshot_hash, last_runtime_snapshot_sent_at
    while True:
        try:
            bots = list(get_bots().values())
            bot_id = str(bots[0].self_id) if bots else str(plugin_config.lily_core_bot_id or "unknown")
            payload = {
                "schema_version": "1.0",
                "instance": instance(bot_id),
                "process_status": "running",
                "connection_status": "connected" if bots else "disconnected",
                "occurred_at": utc_iso(),
                "capabilities": ONEBOT_QQ_CAPABILITIES,
                "ingress_spool": reporter.spool_status(),
                "metadata": {
                    "connected_bots": len(bots),
                    "queue_depth": reporter.queue.qsize(),
                    "dropped": reporter.dropped,
                    "claim_enabled": plugin_config.lily_core_claim_enabled,
                    "claim_failures": reporter.claim_failures,
                    "claim_ack_failures": reporter.claim_ack_failures,
                    "heartbeat_failures": heartbeat_failures,
                    "last_heartbeat_error": last_heartbeat_error,
                    "reporter_workers": reporter.worker_status(),
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            heartbeat_failures += 1
            last_heartbeat_error = type(exc).__name__
            logger.opt(exception=True).error("Lily Core heartbeat iteration failed; the loop will continue")
        await asyncio.sleep(plugin_config.lily_core_heartbeat_seconds)


@driver.on_startup
async def start_bridge() -> None:
    global heartbeat_task, group_inventory_task, directory_snapshot_task
    if not reporter.enabled:
        logger.warning("Lily Core bridge disabled because LILY_CORE_TOKEN is empty")
        return
    await reporter.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="lily-core-heartbeat")
    group_inventory_task = asyncio.create_task(group_inventory_loop(), name="lily-core-group-inventory")
    if plugin_config.lily_core_directory_snapshot_enabled:
        directory_snapshot_task = asyncio.create_task(
            directory_snapshot_loop(), name="lily-core-directory-snapshot"
        )
    logger.info("Lily Core bridge started with fail-open durable event capture")


@driver.on_shutdown
async def stop_bridge() -> None:
    global heartbeat_task, group_inventory_task, directory_snapshot_task
    for task in (heartbeat_task, group_inventory_task, directory_snapshot_task):
        if task:
            task.cancel()
    for task in (heartbeat_task, group_inventory_task, directory_snapshot_task):
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
    heartbeat_task = None
    group_inventory_task = None
    directory_snapshot_task = None
    await reporter.stop()
