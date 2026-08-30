import asyncio
from contextvars import ContextVar
import hashlib
import json
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from functools import wraps
from typing import Any
from uuid import uuid4

import httpx

import nonebot.message as nonebot_message
from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot as OneBotBot
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.exception import MockApiException
from nonebot.matcher import current_event
from nonebot.message import event_postprocessor, event_preprocessor
from pydantic import Field

from nekro_agent.api.core import logger
from nekro_agent.api.plugin import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.schemas.chat_message import ChatMessage
from nekro_agent.schemas.signal import MsgSignal

from .identity import (
    ClaimSuppression,
    NativeIdentityCache,
    OutboundSuppressionTracker,
    ResponseTriggerTracker,
    claim_decision_targets_instance,
    conversation,
    native_identity_cache_key,
)
from .platform_actions import platform_action_event_payload
from .payloads import (
    content_parts,
    message_references,
    message_source_event_id,
    native_message_identity,
    ref_msg_id_from_ext_data,
    safe_platform_id,
)
from .reporter import BackgroundReporter, ReportItem
from .render_retry import (
    RENDER_SUPPRESSED,
    RenderRetryRequired,
    retry_instruction,
    unavailable_instruction,
)

BRIDGE_VERSION = "1.1.1"

plugin = NekroPlugin(
    name="Lily Core Bridge",
    module_name="core_bridge",
    description="Fail-open event, response, and heartbeat reporting to Lily Core",
    version=BRIDGE_VERSION,
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
    TIMEOUT_SECONDS: float = Field(default=0.5, ge=0.05, le=5, title="Legacy HTTP timeout")
    CLAIM_TIMEOUT_SECONDS: float = Field(default=10.0, ge=0.05, le=30, title="Claim HTTP timeout")
    REPORT_TIMEOUT_SECONDS: float = Field(default=10.0, ge=0.05, le=30, title="Report HTTP timeout")
    REPORT_ATTEMPTS: int = Field(default=3, ge=1, le=10, title="Background report attempts")
    REPORT_RETRY_BACKOFF_SECONDS: float = Field(
        default=0.1,
        ge=0,
        le=5,
        title="Background report retry backoff",
    )
    CLAIM_ATTEMPTS: int = Field(default=2, ge=1, le=5, title="Claim and ACK attempts")
    CLAIM_RETRY_BACKOFF_SECONDS: float = Field(
        default=0.1,
        ge=0,
        le=5,
        title="Claim and ACK retry backoff",
    )
    SPOOL_PATH: str = Field(
        default="/home/justin/nekro/plugin_data/Superlily.core_bridge/ingress-spool.sqlite3",
        title="Durable ingress spool path",
    )
    SPOOL_QUOTA_BYTES: int = Field(
        default=268_435_456,
        ge=1_048_576,
        le=4_294_967_296,
        title="Durable ingress spool quota",
    )
    SPOOL_RETENTION_SECONDS: int = Field(
        default=86_400,
        ge=0,
        le=604_800,
        title="Committed record retention",
    )
    SPOOL_MAX_RECORD_BYTES: int = Field(
        default=1_048_576,
        ge=65_536,
        le=8_388_608,
        title="Maximum durable event bytes",
    )
    CLAIM_ENABLED: bool = Field(default=False, title="Enable fail-open Lily Core claims")
    RENDER_ENABLED: bool = Field(default=False, title="Enable Core document rendering")
    RENDER_ALL_GROUPS: bool = Field(
        default=False,
        title="Enable Core document rendering in every group chat (overrides canary keys)",
    )
    RENDER_CANARY_CHAT_KEYS: str = Field(
        default="",
        title="Comma-separated exact Nekro chat keys allowed to render",
    )
    RENDER_TIMEOUT_SECONDS: float = Field(default=40.0, ge=5, le=120, title="Render timeout")
    AGENT_ENABLED: bool = Field(default=False, title="Enable Core-owned Agent entry")
    AGENT_CANARY_CHAT_KEYS: str = Field(
        default="",
        title="Comma-separated exact Nekro chat keys allowed to use Core Agent",
    )
    AGENT_DELIVERY_POLL_SECONDS: float = Field(
        default=0.5,
        ge=0.1,
        le=10,
        title="Core Agent text-delivery poll interval",
    )


config: BridgeConfig = plugin.get_config(BridgeConfig)
reporter = BackgroundReporter(
    config.CORE_URL,
    config.CORE_TOKEN,
    config.QUEUE_SIZE,
    config.CLAIM_TIMEOUT_SECONDS,
    config.REPORT_TIMEOUT_SECONDS,
    config.REPORT_ATTEMPTS,
    config.REPORT_RETRY_BACKOFF_SECONDS,
    config.CLAIM_ATTEMPTS,
    config.CLAIM_RETRY_BACKOFF_SECONDS,
    config.SPOOL_PATH,
    config.SPOOL_QUOTA_BYTES,
    config.SPOOL_RETENTION_SECONDS,
    config.SPOOL_MAX_RECORD_BYTES,
)
heartbeat_task: asyncio.Task | None = None
agent_delivery_task: asyncio.Task | None = None
heartbeat_failures = 0
last_heartbeat_error: str | None = None
_TRIGGER_TRACKER_ATTR = "_superlily_response_trigger_tracker_v2"
_TRIGGER_BINDERS_ATTR = "_superlily_response_trigger_binders_v1"
_SUPPRESSION_TRACKER_ATTR = "_superlily_outbound_suppression_tracker_v1"
_EVENT_SUPPRESSIONS_ATTR = "_superlily_event_suppressions_v1"
_EVENT_SEND_ATTEMPTS_ATTR = "_superlily_event_send_attempts_v1"
_ACTIVE_CLAIM_EVENTS_ATTR = "_superlily_active_claim_events_v1"
api_started: dict[int, float] = {}
blocked_api_calls: dict[int, ClaimSuppression] = {}
_render_send_receipt: ContextVar[asyncio.Future[dict[str, str | None]] | None] = ContextVar(
    "superlily_render_send_receipt",
    default=None,
)
TRIGGER_BIND_MAX_WAIT_SECONDS = 3600.0
TRIGGER_BIND_POLL_SECONDS = 0.1
ONEBOT_QQ_CAPABILITIES = {
    "profile": "onebot_v11.qq.v1",
    "supported": ["mention", "reply", "send_image", "send_text"],
    "limits": {},
}

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


def _render_conversation(chat_key: str) -> dict[str, str] | None:
    prefix = "onebot_v11-"
    if not chat_key.startswith(prefix):
        return None
    remainder = chat_key[len(prefix) :]
    kind, separator, conversation_id = remainder.partition("_")
    if separator and kind in {"group", "private"} and conversation_id:
        return {"type": kind, "id": conversation_id, "name": None}
    return None


def _render_request_context(chat_key: str) -> tuple[str | None, str]:
    conv = _render_conversation(chat_key)
    source_event_id = _current_task_trigger(conv) if conv is not None else None
    task_token = _task_token(conv) if conv is not None else None
    context_key = source_event_id or (f"task:{task_token}" if task_token is not None else f"unbound:{uuid4()}")
    return source_event_id, context_key


def _version_render_blocks(blocks: Any) -> list[dict[str, Any]]:
    if not isinstance(blocks, list):
        raise ValueError("blocks must be a list")
    counter = 0

    def visit(block: Any) -> dict[str, Any]:
        nonlocal counter
        if not isinstance(block, dict):
            raise ValueError("render blocks must be objects")
        counter += 1
        normalized = dict(block)
        normalized["node_id"] = f"n{counter:03d}"
        kind = normalized.get("kind")
        if kind == "group":
            normalized["blocks"] = [visit(child) for child in normalized.get("blocks", [])]
        elif kind == "alternative":
            options = normalized.get("options")
            if not isinstance(options, list):
                raise ValueError("alternative options must be a list")
            normalized_options = []
            for option in options:
                if not isinstance(option, dict):
                    raise ValueError("alternative options must be objects")
                normalized_option = dict(option)
                normalized_option["blocks"] = [
                    visit(child) for child in normalized_option.get("blocks", [])
                ]
                normalized_options.append(normalized_option)
            normalized["options"] = normalized_options
        return normalized

    return [visit(block) for block in blocks]


def _render_canary_chat_keys() -> frozenset[str]:
    return frozenset(
        item.strip() for item in config.RENDER_CANARY_CHAT_KEYS.split(",") if item.strip()
    )


def _agent_canary_chat_keys() -> frozenset[str]:
    return frozenset(
        item.strip() for item in config.AGENT_CANARY_CHAT_KEYS.split(",") if item.strip()
    )


def _agent_entry_allowed(message: ChatMessage) -> bool:
    return bool(
        config.AGENT_ENABLED
        and config.CORE_TOKEN
        and message.is_tome
        and message.chat_key in _agent_canary_chat_keys()
    )


def _render_allowed(ctx: AgentCtx) -> bool:
    if not (config.RENDER_ENABLED and config.CORE_TOKEN):
        return False
    if config.RENDER_ALL_GROUPS:
        conversation = _render_conversation(ctx.chat_key)
        return conversation is not None and conversation["type"] == "group"
    return ctx.chat_key in _render_canary_chat_keys()


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


def _trigger_tracker() -> ResponseTriggerTracker:
    tracker = getattr(nonebot_message, _TRIGGER_TRACKER_ATTR, None)
    if tracker is None:
        tracker = ResponseTriggerTracker()
        setattr(nonebot_message, _TRIGGER_TRACKER_ATTR, tracker)
    return tracker


def _trigger_binders() -> dict[tuple[str, str], asyncio.Task]:
    binders = getattr(nonebot_message, _TRIGGER_BINDERS_ATTR, None)
    if binders is None:
        binders = {}
        setattr(nonebot_message, _TRIGGER_BINDERS_ATTR, binders)
    return binders


def _suppression_tracker() -> OutboundSuppressionTracker:
    tracker = getattr(nonebot_message, _SUPPRESSION_TRACKER_ATTR, None)
    if tracker is None:
        tracker = OutboundSuppressionTracker()
        setattr(nonebot_message, _SUPPRESSION_TRACKER_ATTR, tracker)
    return tracker


def _event_suppressions() -> dict[int, str]:
    active = getattr(nonebot_message, _EVENT_SUPPRESSIONS_ATTR, None)
    if active is None:
        active = {}
        setattr(nonebot_message, _EVENT_SUPPRESSIONS_ATTR, active)
    return active


def _event_send_attempts() -> dict[int, set[tuple[str, str]]]:
    attempts = getattr(nonebot_message, _EVENT_SEND_ATTEMPTS_ATTR, None)
    if attempts is None:
        attempts = {}
        setattr(nonebot_message, _EVENT_SEND_ATTEMPTS_ATTR, attempts)
    return attempts


def _active_claim_events() -> set[int]:
    active = getattr(nonebot_message, _ACTIVE_CLAIM_EVENTS_ATTR, None)
    if active is None:
        active = set()
        setattr(nonebot_message, _ACTIVE_CLAIM_EVENTS_ATTR, active)
    return active


def _chat_key(conv: dict[str, Any]) -> str:
    return f"onebot_v11-{conv['type']}_{conv['id']}"


def _task_token(conv: dict[str, Any]) -> int | None:
    try:
        from nekro_agent.services.message_service import message_service

        task = message_service.running_tasks.get(_chat_key(conv))
        return id(task) if task is not None and not task.done() else None
    except Exception:
        return None


async def _bind_trigger_when_task_starts(
    conv: dict[str, Any],
    previous_task_token: int | None,
) -> None:
    key = (str(conv.get("type", "unknown")), str(conv.get("id", "unknown")))
    current_task = asyncio.current_task()
    deadline = time.monotonic() + TRIGGER_BIND_MAX_WAIT_SECONDS
    try:
        while time.monotonic() < deadline:
            task_token = _task_token(conv)
            if task_token is not None:
                if previous_task_token is None or task_token != previous_task_token:
                    _trigger_tracker().observe_task(conv, task_token)
                    return
                # A pending source may wait behind a long-running task. Keep
                # the authoritative current token alive until the scheduler
                # replaces it, so the pending source can transition exactly
                # once to the next task.
                _trigger_tracker().observe_task(conv, task_token)
            await asyncio.sleep(TRIGGER_BIND_POLL_SECONDS)
    finally:
        if _trigger_binders().get(key) is current_task:
            _trigger_binders().pop(key, None)


def _schedule_trigger_binding(
    conv: dict[str, Any],
    previous_task_token: int | None,
) -> None:
    key = (str(conv.get("type", "unknown")), str(conv.get("id", "unknown")))
    existing = _trigger_binders().pop(key, None)
    if existing is not None:
        existing.cancel()
    task = asyncio.create_task(
        _bind_trigger_when_task_starts(dict(conv), previous_task_token),
        name=f"superlily-trigger-bind:{key[0]}:{key[1]}",
    )
    _trigger_binders()[key] = task


def _remember_trigger(
    conv: dict[str, Any],
    source_id: str,
    should_remember: bool,
    *,
    bind_task: bool = False,
) -> None:
    if not should_remember:
        return
    task_token = _task_token(conv)
    _trigger_tracker().remember(conv, source_id, task_token)
    if bind_task:
        _schedule_trigger_binding(conv, task_token)


def _current_task_trigger(conv: dict[str, Any]) -> str | None:
    return _trigger_tracker().source_for_response(conv, _task_token(conv))


async def _observe_user_message(message: ChatMessage) -> tuple[dict[str, Any], str]:
    conv = conversation(message.chat_key, message.chat_type)
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
    metadata: dict[str, Any] = {
        "is_tome": bool(message.is_tome),
        "chat_key": message.chat_key,
    }
    if message.is_tome:
        metadata["agent_trigger_kind"] = "reply" if ref_msg_id else "mention"
    if native_identity:
        metadata["native_identity"] = native_identity
    occurred_at = utc_iso(message.send_timestamp)
    sender_id = str(message.platform_userid or message.sender_id)
    source_id = message_source_event_id(
        conv,
        message.message_id,
        native_identity,
        sender_id=sender_id,
        occurred_at=occurred_at,
    )
    _remember_trigger(
        conv,
        source_id,
        bool(message.is_tome),
        bind_task=bool(message.is_tome),
    )
    payload = {
        "schema_version": "1.0",
        "source_event_id": source_id,
        "instance": instance(),
        "event_type": "message",
        "conversation": conv,
        "sender": {
            "id": sender_id,
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
        "occurred_at": occurred_at,
        "raw": None,
        "metadata": metadata,
    }
    idempotency_key = stable_key(config.INSTANCE_ID, source_id)
    return payload, idempotency_key


@plugin.mount_on_user_message()
async def observe_user_message(_: AgentCtx, message: ChatMessage) -> MsgSignal:
    try:
        payload, idempotency_key = await _observe_user_message(message)
        if config.CLAIM_ENABLED:
            claim = await reporter.request_claim(payload, idempotency_key)
            if claim is None:
                reporter.enqueue(ReportItem("/v1/events", payload, idempotency_key))
            if claim_decision_targets_instance(claim, config.INSTANCE_ID):
                _remember_trigger(
                    payload["conversation"],
                    payload["source_event_id"],
                    True,
                    bind_task=True,
                )
            if claim and claim.get("enforced") is True and claim.get("action") == "deny":
                # Preserve the source in the task tracker: if another plugin's
                # FORCE_TRIGGER overrides BLOCK_TRIGGER, any later agent send
                # is still attributable to this denied message and the API
                # guard below suppresses it.
                _remember_trigger(payload["conversation"], payload["source_event_id"], True)
                suppression, authoritative = _install_claim_suppression(payload, claim)
                acknowledged = False
                if authoritative and suppression is not None:
                    acknowledged = await reporter.acknowledge_claim(suppression.claim_id)
                    _suppression_tracker().set_acknowledged(
                        payload["conversation"],
                        payload["source_event_id"],
                        acknowledged,
                    )
                logger.info(
                    f"Lily Core claim denied event {claim.get('source_event_id')} "
                    f"({claim.get('reason')}; guard={authoritative}; "
                    f"acknowledged={acknowledged})"
                )
                return MsgSignal.BLOCK_TRIGGER
        else:
            reporter.enqueue(ReportItem("/v1/events", payload, idempotency_key))
        if _agent_entry_allowed(message):
            interaction = await reporter.request_agent_interaction(
                payload,
                idempotency_key,
                capture_event=False,
            )
            if interaction is not None and interaction.get("accepted") is True:
                logger.info(
                    "Lily Core Agent accepted exact canary interaction "
                    f"{interaction.get('interaction_id')}"
                )
                return MsgSignal.BLOCK_TRIGGER
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
_PLATFORM_ACTION_HOOK_ATTR = "_superlily_platform_action_hook_v1"


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


def _api_conversation(data: dict[str, Any]) -> dict[str, str]:
    if data.get("group_id") is not None:
        return {"id": str(data["group_id"]), "type": "group"}
    return {
        "id": str(data.get("user_id") or data.get("target_id") or "unknown"),
        "type": "private",
    }


def _install_claim_suppression(
    payload: dict[str, Any],
    claim: dict[str, Any],
) -> tuple[ClaimSuppression | None, bool]:
    """Install exact event/task send guards before a deny can be ACKed."""

    conv = payload["conversation"]
    source_event_id = str(payload.get("source_event_id") or "")
    claim_id = str(claim.get("claim_id") or "")
    if not source_event_id or not claim_id:
        return None, False
    suppression = _suppression_tracker().install(
        conv,
        source_event_id,
        claim_id,
        str(claim.get("reason") or "assigned_to_another_instance"),
    )

    event = current_event.get(None)
    if event is None or id(event) not in _active_claim_events():
        return suppression, False
    raw = _event_dict(event)
    if raw.get("post_type") != "message":
        return suppression, False
    event_conv = _onebot_conversation(raw)
    if (
        event_conv["type"] != str(conv.get("type"))
        or event_conv["id"] != str(conv.get("id"))
    ):
        return suppression, False
    conversation_key = (event_conv["type"], event_conv["id"])
    prior_send_seen = conversation_key in _event_send_attempts().get(id(event), set())
    _event_suppressions()[id(event)] = source_event_id
    # A plugin that ran before this bridge may already have attempted a send.
    # Such an event cannot truthfully certify exclusive suppression, even
    # though every later send is guarded.
    return suppression, not prior_send_seen


def _active_event_suppression(conv: dict[str, Any]) -> ClaimSuppression | None:
    event = current_event.get(None)
    if event is None:
        return None
    source_event_id = _event_suppressions().get(id(event))
    return _suppression_tracker().match(conv, source_event_id)


def _match_claim_suppression(data: dict[str, Any]) -> ClaimSuppression | None:
    conv = _api_conversation(data)
    event_suppression = _active_event_suppression(conv)
    if event_suppression is not None:
        return event_suppression
    return _suppression_tracker().match(conv, _current_task_trigger(conv))


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


if not getattr(nonebot_message, _PLATFORM_ACTION_HOOK_ATTR, False):

    @event_preprocessor
    async def observe_platform_action(bot: OneBotBot, event: OneBotEvent) -> None:
        try:
            raw = _event_dict(event)
            conv = _onebot_conversation(raw)
            payload = platform_action_event_payload(
                raw,
                conv,
                instance(bot.self_id),
                event_type=(
                    event.get_event_name()
                    if hasattr(event, "get_event_name")
                    else f"notice.{raw.get('notice_type', 'unknown')}"
                ),
                fallback_occurred_at=utc_iso(),
                to_me=bool(getattr(event, "to_me", False)),
            )
            if payload is None:
                return
            source_id = payload["source_event_id"]
            reporter.enqueue(
                ReportItem(
                    "/v1/events",
                    payload,
                    stable_key(config.INSTANCE_ID, source_id),
                )
            )
        except Exception:
            logger.exception("Lily Core platform-action observation failed open")

    setattr(nonebot_message, _PLATFORM_ACTION_HOOK_ATTR, True)


if not getattr(nonebot_message, "_superlily_suppression_cleanup_hook_v1", False):

    @event_preprocessor
    async def start_claim_event_suppression(event: OneBotEvent) -> None:
        if _event_dict(event).get("post_type") == "message":
            _active_claim_events().add(id(event))

    @event_postprocessor
    async def clear_claim_event_suppression(event: OneBotEvent) -> None:
        _active_claim_events().discard(id(event))
        _event_suppressions().pop(id(event), None)
        _event_send_attempts().pop(id(event), None)

    nonebot_message._superlily_suppression_cleanup_hook_v1 = True


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
            attachments.append(
                {
                    "type": segment_type,
                    "name": data.get("name") or data.get("file_name"),
                    "platform_id": safe_platform_id(data.get("file")),
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
        source_response = (
            f"qq:{bot.self_id}:message:{message_id}"
            if message_id is not None
            else f"qq:{bot.self_id}:sent-attempt:{uuid4()}"
        )
        trigger_source_event_id = _current_task_trigger(conv)
        reply_id = None
        for segment in segments:
            if segment.get("type") == "reply":
                reply_id = str(segment.get("data", {}).get("id") or "") or None
                break
        payload = {
            "schema_version": "1.0",
            "source_response_id": source_response,
            "instance": instance(bot.self_id),
            "trigger_source_event_id": trigger_source_event_id,
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
            "metadata": {
                "trigger_attribution": "task_context" if trigger_source_event_id else None,
                "completion_status": "succeeded",
            },
        }
        reporter.enqueue(
            ReportItem("/v1/responses", payload, stable_key(config.INSTANCE_ID, source_response))
        )

    nonebot_message._superlily_message_sent_hook = True


if not getattr(OneBotBot, "_superlily_claim_send_guard_v1", False):

    @OneBotBot.on_calling_api
    async def suppress_denied_send(
        _: OneBotBot,
        api: str,
        data: dict[str, Any],
    ) -> None:
        if not api.startswith("send_"):
            return
        api_started[id(data)] = time.monotonic()
        conv = _api_conversation(data)
        event = current_event.get(None)
        if event is not None and id(event) in _active_claim_events():
            _event_send_attempts().setdefault(id(event), set()).add(
                (conv["type"], conv["id"])
            )
        suppression = _match_claim_suppression(data)
        if suppression is None:
            return
        blocked_api_calls[id(data)] = suppression
        raise MockApiException({"message_id": -1})

    OneBotBot._superlily_claim_send_guard_v1 = True


if not getattr(OneBotBot, "_superlily_send_result_hook_v2", False):

    @OneBotBot.on_called_api
    async def observe_send_result(
        bot: OneBotBot,
        exception: Exception | None,
        api: str,
        data: dict[str, Any],
        result: Any,
    ) -> None:
        if not api.startswith("send_"):
            return
        started = api_started.pop(id(data), None)
        suppression = blocked_api_calls.pop(id(data), None)
        render_receipt = _render_send_receipt.get()
        if render_receipt is not None and not render_receipt.done():
            message_id = result.get("message_id") if isinstance(result, dict) else None
            error_text = str(exception).lower()
            if suppression is not None:
                delivery_result = {
                    "outcome": "failed",
                    "platform_message_id": None,
                    "safe_error_code": "blocked_by_core_claim",
                }
            elif exception is None and message_id is not None:
                delivery_result = {
                    "outcome": "succeeded",
                    "platform_message_id": str(message_id),
                    "safe_error_code": None,
                }
            elif exception is None:
                delivery_result = {
                    "outcome": "ambiguous",
                    "platform_message_id": None,
                    "safe_error_code": "platform_message_id_unavailable",
                }
            elif "timeout" in error_text or "timed out" in error_text:
                delivery_result = {
                    "outcome": "ambiguous",
                    "platform_message_id": None,
                    "safe_error_code": "platform_completion_unknown",
                }
            else:
                delivery_result = {
                    "outcome": "failed",
                    "platform_message_id": None,
                    "safe_error_code": "platform_send_failed",
                }
            render_receipt.set_result(delivery_result)
        if exception is None and suppression is None:
            # Successful platform sends are recorded from confirmed
            # message_sent events; do not duplicate them here.
            return
        conv = {**_api_conversation(data), "name": None}
        source_response = (
            f"qq:{bot.self_id}:suppressed-attempt:{uuid4()}"
            if suppression is not None
            else f"qq:{bot.self_id}:failed-attempt:{uuid4()}"
        )
        trigger_source_event_id = (
            suppression.source_event_id
            if suppression is not None
            else _current_task_trigger(conv)
        )
        text, segments, attachments = _onebot_message_parts(data.get("message"))
        error_text = str(exception).lower()
        latency_ms = int((time.monotonic() - started) * 1000) if started is not None else None
        if suppression is not None:
            completion_status = "suppressed"
        elif "timeout" in error_text or "timed out" in error_text:
            completion_status = "ambiguous"
        else:
            completion_status = "failed"
        payload = {
            "schema_version": "1.0",
            "source_response_id": source_response,
            "instance": instance(bot.self_id),
            "trigger_source_event_id": trigger_source_event_id,
            "response_type": api,
            "conversation": conv,
            "text": text,
            "segments": segments,
            "attachments": attachments,
            "success": False,
            "error": "blocked_by_core_claim" if suppression is not None else str(exception),
            "latency_ms": latency_ms,
            "occurred_at": utc_iso(),
            "raw": None,
            "metadata": {
                "claim_send_suppressed": suppression is not None,
                "claim_id": suppression.claim_id if suppression is not None else None,
                "claim_reason": suppression.reason if suppression is not None else None,
                "claim_acknowledged": (
                    suppression.acknowledged if suppression is not None else None
                ),
                "trigger_attribution": (
                    "claim_suppression"
                    if suppression is not None
                    else "task_context"
                    if trigger_source_event_id
                    else None
                ),
                "completion_status": completion_status,
            },
        }
        reporter.enqueue(
            ReportItem("/v1/responses", payload, stable_key(config.INSTANCE_ID, source_response))
        )

    OneBotBot._superlily_send_result_hook_v2 = True


async def heartbeat_loop() -> None:
    global heartbeat_failures, last_heartbeat_error
    while True:
        try:
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
                        "capabilities": ONEBOT_QQ_CAPABILITIES,
                        "ingress_spool": reporter.spool_status(),
                        "metadata": {
                            "connected_bots": len(bots),
                            "queue_depth": reporter.queue.qsize(),
                            "dropped": reporter.dropped,
                            "claim_enabled": config.CLAIM_ENABLED,
                            "claim_failures": reporter.claim_failures,
                            "claim_ack_failures": reporter.claim_ack_failures,
                            "heartbeat_failures": heartbeat_failures,
                            "last_heartbeat_error": last_heartbeat_error,
                            "reporter_workers": reporter.worker_status(),
                            "bridge_version": BRIDGE_VERSION,
                        },
                    },
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            heartbeat_failures += 1
            last_heartbeat_error = type(exc).__name__
            logger.exception(
                "Lily Core heartbeat iteration failed; the loop will continue"
            )
        await asyncio.sleep(config.HEARTBEAT_SECONDS)


async def _deliver_agent_text(lease: dict[str, Any]) -> None:
    intent_id = str(lease.get("intent_id") or "")
    content = str(lease.get("text") or "")
    expected_hash = str(lease.get("content_sha256") or "")
    outcome = "failed"
    platform_message_id: str | None = None
    safe_error_code: str | None = "delivery_payload_invalid"
    if (
        not intent_id
        or not content
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_hash
    ):
        pass
    else:
        bots = list(get_bots().values())
        bot = bots[0] if bots else None
        if not isinstance(bot, OneBotBot):
            safe_error_code = "onebot_unavailable"
        else:
            message = OneBotMessage()
            reply_id = lease.get("reply_to_platform_message_id")
            if isinstance(reply_id, str) and reply_id:
                message += OneBotMessageSegment.reply(reply_id)
            message += OneBotMessageSegment.text(content)
            future: asyncio.Future[dict[str, str | None]] = (
                asyncio.get_running_loop().create_future()
            )
            receipt_token = _render_send_receipt.set(future)
            try:
                if lease.get("conversation_type") == "group":
                    await bot.send_group_msg(
                        group_id=int(str(lease["conversation_id"])),
                        message=message,
                    )
                elif lease.get("conversation_type") == "private":
                    await bot.send_private_msg(
                        user_id=int(str(lease["conversation_id"])),
                        message=message,
                    )
                else:
                    raise ValueError("unsupported Agent delivery conversation type")
                receipt = await asyncio.wait_for(asyncio.shield(future), timeout=2.0)
                outcome = str(receipt.get("outcome") or "ambiguous")
                platform_message_id = receipt.get("platform_message_id")
                safe_error_code = receipt.get("safe_error_code")
            except asyncio.TimeoutError:
                outcome = "ambiguous"
                platform_message_id = None
                safe_error_code = "platform_completion_unknown"
            except Exception:
                if future.done() and not future.cancelled():
                    receipt = future.result()
                    outcome = str(receipt.get("outcome") or "failed")
                    platform_message_id = receipt.get("platform_message_id")
                    safe_error_code = receipt.get("safe_error_code")
                else:
                    outcome = "failed"
                    platform_message_id = None
                    safe_error_code = "platform_send_failed"
            finally:
                _render_send_receipt.reset(receipt_token)
    await reporter.complete_agent_delivery(
        intent_id,
        {
            "schema_version": "1.0",
            "instance_id": config.INSTANCE_ID,
            "fence": lease.get("fence"),
            "lease_token": lease.get("lease_token"),
            "outcome": outcome,
            "platform_message_id": platform_message_id,
            "safe_error_code": safe_error_code,
        },
    )


async def agent_delivery_loop() -> None:
    while True:
        try:
            if config.AGENT_ENABLED and get_bots():
                lease = await reporter.lease_agent_delivery(config.INSTANCE_ID)
                if lease is not None:
                    await _deliver_agent_text(lease)
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Lily Core Agent delivery iteration failed")
        await asyncio.sleep(config.AGENT_DELIVERY_POLL_SECONDS)


@plugin.mount_prompt_inject_method(
    name="Lily Core document renderer policy",
    description="Use the reviewed renderer for mixed Chinese text and mathematics in enabled group chats",
)
async def render_prompt_policy(_ctx: AgentCtx) -> str:
    if not _render_allowed(_ctx):
        return ""
    return """
本频道已启用 Lily Core 统一文档渲染器。需要发送含中文长文、公式、题目列表的图片时，
只需调用 `submit_rendered_markdown(markdown_text)`，传入一整段普通 Markdown，不要构造 JSON。
该方法已作为全局 predefined method 注入沙盒，直接调用；禁止 import `lily_core_bridge` 或任何模块来获取它。
在 Python 沙盒代码中，Markdown 必须放进 `r'''...'''` 原始三引号字符串，避免
`\\alpha`、`\\frac`、`\\bar`、`\\to`、`\\text` 等 TeX 命令被 Python 转义损坏。
可直接使用 #/## 标题、自然段、- 或 1. 列表、> 引用、``` 代码围栏和 Markdown 表格；
用 **文字** 加粗，用单个美元符号写行内公式，例如
`**结论：** 已知 $f(x)=x^3+px^2+qx+r$。`；矩阵或长推导才用成对的 $$ 独占成行。
链接、Markdown 图片和原始 HTML 只会显示为文字，不会访问网络或本地文件。
不要使用 PIL/ImageDraw 或 Matplotlib 的 text/annotate 自行排版文字和公式；Matplotlib 只用于真正的数据图表。
该方法会直接发送渲染成品，调用成功后不要重复发送同一内容。
方法成功才表示成品已经发送。内容编译错误会终止当前脚本，并在下一次模型迭代中返回
有界的真实错误类别、不可用命令和 Markdown node_id；根据该诊断修改 Markdown 后再调用。
不得把失败的 Markdown 自动改成普通长文本发送。任何 `INTERNAL_RENDER_*` 状态都属于
内部控制信息，绝不能向用户提及，也不得据此重复发送。
""".strip()


async def _deliver_render_request(
    _ctx: AgentCtx,
    *,
    endpoint: str,
    payload: dict[str, Any],
    request_context: str,
) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    render_key = stable_key(_ctx.chat_key, request_context, canonical)
    headers = {
        "Authorization": f"Bearer {config.CORE_TOKEN}",
        "Idempotency-Key": f"nekro-render:{render_key}",
    }
    timeout = httpx.Timeout(
        config.RENDER_TIMEOUT_SECONDS,
        connect=min(5.0, config.RENDER_TIMEOUT_SECONDS),
    )
    try:
        async with httpx.AsyncClient(base_url=config.CORE_URL, timeout=timeout) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            receipt = response.json()
            expected_hash = receipt.get("content_sha256")
            content_path = receipt.get("content_path")
            delivery_plan_id = receipt.get("delivery_plan_id")
            delivery_plan = receipt.get("delivery_plan")
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or not isinstance(content_path, str)
                or not content_path.startswith("/v1/render-artifacts/")
                or not isinstance(delivery_plan_id, str)
                or not isinstance(delivery_plan, dict)
            ):
                raise RenderRetryRequired(unavailable_instruction())
            intent_response = await client.post(
                f"/v1/render-artifacts/{receipt['artifact_id']}/delivery-intents",
                json={
                    "instance_id": config.INSTANCE_ID,
                    "delivery_plan_id": delivery_plan_id,
                    "idempotency_key": f"nekro-delivery:{render_key}",
                },
                headers={"Authorization": f"Bearer {config.CORE_TOKEN}"},
            )
            intent_response.raise_for_status()
            intent = intent_response.json()
            if not intent.get("should_send"):
                return RENDER_SUPPRESSED

            selected_family = delivery_plan.get("selected_family")
            send_receipt: dict[str, str | None]
            future: asyncio.Future[dict[str, str | None]] = (
                asyncio.get_running_loop().create_future()
            )
            receipt_token = _render_send_receipt.set(future)
            try:
                if selected_family == "image":
                    artifact_response = await client.get(
                        content_path,
                        headers={"Authorization": f"Bearer {config.CORE_TOKEN}"},
                    )
                    artifact_response.raise_for_status()
                    content = artifact_response.content
                    if (
                        not content.startswith(b"\x89PNG\r\n\x1a\n")
                        or hashlib.sha256(content).hexdigest() != expected_hash
                    ):
                        send_receipt = {
                            "outcome": "failed",
                            "platform_message_id": None,
                            "safe_error_code": "artifact_integrity_failure",
                        }
                    else:
                        sandbox_path = await _ctx.fs.mixed_forward_file(
                            content,
                            file_name=f"lily-render-{receipt['artifact_id']}.png",
                        )
                        await _ctx.send_image(sandbox_path)
                        send_receipt = await asyncio.wait_for(
                            asyncio.shield(future), timeout=2.0
                        )
                elif selected_family == "text" and isinstance(
                    delivery_plan.get("fallback_text"), str
                ):
                    await _ctx.send_text(delivery_plan["fallback_text"])
                    send_receipt = await asyncio.wait_for(
                        asyncio.shield(future), timeout=2.0
                    )
                else:
                    send_receipt = {
                        "outcome": "failed",
                        "platform_message_id": None,
                        "safe_error_code": "delivery_plan_invalid",
                    }
            except asyncio.TimeoutError:
                send_receipt = {
                    "outcome": "ambiguous",
                    "platform_message_id": None,
                    "safe_error_code": "platform_completion_unknown",
                }
            except Exception:
                if future.done() and not future.cancelled():
                    send_receipt = future.result()
                else:
                    send_receipt = {
                        "outcome": "failed",
                        "platform_message_id": None,
                        "safe_error_code": "platform_send_failed",
                    }
            finally:
                _render_send_receipt.reset(receipt_token)

            completion_response = await client.post(
                f"/v1/render-delivery-intents/{intent['intent_id']}/complete",
                json={
                    "instance_id": config.INSTANCE_ID,
                    **send_receipt,
                },
                headers={"Authorization": f"Bearer {config.CORE_TOKEN}"},
            )
            completion_response.raise_for_status()
            if send_receipt["outcome"] == "succeeded":
                return (
                    "INTERNAL_RENDER_DELIVERED. The answer is already delivered. "
                    "Do not send it again or mention internal rendering status."
                )
            if send_receipt["outcome"] == "ambiguous":
                return (
                    "INTERNAL_RENDER_DELIVERY_UNCONFIRMED. Do not retry or send a "
                    "fallback because that could duplicate a platform message. Never "
                    "mention internal status."
                )
            return (
                "INTERNAL_RENDER_DELIVERY_FAILED. Do not retry this platform action. "
                "Continue without mentioning internal status."
            )
    except httpx.HTTPStatusError as exc:
        error_code = exc.response.headers.get(
            "X-Render-Error-Code",
            "render_request_rejected",
        )
        logger.warning(
            f"Lily Core rejected render request with status "
            f"{exc.response.status_code} code={error_code}"
        )
        content_error = (
            endpoint == "/v1/markdown-documents"
            and (
                exc.response.status_code == 422
                or error_code
                in {"renderer_content_error", "renderer_execution_failed"}
            )
        )
        if content_error:
            diagnostic: object = None
            try:
                body = exc.response.json()
                detail = body.get("detail") if isinstance(body, dict) else None
                if isinstance(detail, dict):
                    diagnostic = detail.get("diagnostic")
            except ValueError:
                pass
            raise RenderRetryRequired(
                retry_instruction(error_code, diagnostic)
            ) from exc
        raise RenderRetryRequired(unavailable_instruction()) from exc
    except RenderRetryRequired:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Lily Core render request failed safely")
        raise RenderRetryRequired(unavailable_instruction()) from exc
    except Exception as exc:
        logger.exception("Lily Core render delivery failed")
        raise RenderRetryRequired(unavailable_instruction()) from exc


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="submit_rendered_markdown",
    description="Globally injected predefined method; call directly without import. Render and send one Markdown document",
)
async def submit_rendered_markdown(_ctx: AgentCtx, markdown_text: str) -> str:
    """全局预定义方法，直接调用且禁止 import；无需选择段落类型或构造 JSON。

    Args:
        markdown_text: 一整段普通 Markdown，支持标题、列表、加粗、代码和数学公式。
    """

    if not _render_allowed(_ctx):
        return "统一文档渲染器未对当前频道开放。"
    if not isinstance(markdown_text, str) or not markdown_text.strip():
        return "Markdown 内容不能为空。"
    source_event_id, request_context = _render_request_context(_ctx.chat_key)
    return await _deliver_render_request(
        _ctx,
        endpoint="/v1/markdown-documents",
        payload={
            "schema_version": "1.0",
            "instance_id": config.INSTANCE_ID,
            "conversation_key": _ctx.chat_key,
            "source_event_id": source_event_id,
            "markdown": markdown_text,
        },
        request_context=request_context,
    )


@plugin.mount_sandbox_method(
    SandboxMethodType.BEHAVIOR,
    name="submit_render_document",
    description="Legacy structured RenderDocument JSON compatibility entrypoint",
)
async def submit_render_document(_ctx: AgentCtx, document_json: str) -> str:
    """保留给旧会话的结构化兼容入口；新会话应提交普通 Markdown。

    Args:
        document_json: JSON 对象字符串，只能包含可选 title 与 blocks。
    """

    if not _render_allowed(_ctx):
        return "统一文档渲染器未对当前频道开放。"
    try:
        raw = json.loads(document_json)
        if not isinstance(raw, dict) or not set(raw).issubset({"title", "blocks"}):
            return "文档结构无效：只允许 title 和 blocks。"
        source_event_id, request_context = _render_request_context(_ctx.chat_key)
        payload = {
            "schema_version": "1.3",
            "instance_id": config.INSTANCE_ID,
            "conversation_key": _ctx.chat_key,
            "source_event_id": source_event_id,
            "blocks": _version_render_blocks(raw.get("blocks")),
        }
        if raw.get("title") is not None:
            payload["title"] = raw["title"]
        return await _deliver_render_request(
            _ctx,
            endpoint="/v1/render-documents",
            payload=payload,
            request_context=request_context,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return "文档结构无效，请按约定的 blocks JSON 重试。"


@plugin.mount_init_method()
async def init_bridge() -> None:
    global heartbeat_task, agent_delivery_task
    if not reporter.enabled:
        logger.warning("Lily Core bridge disabled because CORE_TOKEN is empty")
        return
    await reporter.start()
    heartbeat_task = asyncio.create_task(heartbeat_loop(), name="nekro-lily-core-heartbeat")
    agent_delivery_task = asyncio.create_task(
        agent_delivery_loop(),
        name="nekro-lily-core-agent-delivery",
    )
    logger.info("Lily Core bridge started with fail-open durable event capture")


@plugin.mount_cleanup_method()
async def cleanup_bridge() -> None:
    global heartbeat_task, agent_delivery_task
    for task in (heartbeat_task, agent_delivery_task):
        if task:
            task.cancel()
    for task in (heartbeat_task, agent_delivery_task):
        if not task:
            continue
        try:
            await task
        except asyncio.CancelledError:
            pass
    heartbeat_task = None
    agent_delivery_task = None
    await reporter.stop()

__all__ = ["plugin"]
