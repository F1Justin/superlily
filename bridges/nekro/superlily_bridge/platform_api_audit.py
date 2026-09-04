from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4


_SIDE_EFFECT_PREFIXES = (
    "set_",
    "_set_",
    "delete_",
    "del_",
    "_del_",
    "upload_",
    "create_",
    "move_",
    "trans_",
    "rename_",
    "mark_",
    "_mark_",
    "forward_",
    "click_",
    "do_",
)
_SIDE_EFFECT_EXACT = frozenset(
    {
        "_send_group_notice",
        ".handle_quick_operation",
        "bot_exit",
        "clean_cache",
        "download_file",
        "friend_poke",
        "group_poke",
        "send_group_sign",
        "send_like",
        "send_packet",
        "send_poke",
        "ArkShareGroup",
        "ArkSharePeer",
    }
)
_SAFE_TEXT_FIELDS = frozenset(
    {
        "card",
        "comment",
        "content",
        "folder_name",
        "group_name",
        "name",
        "nickname",
        "notice_id",
        "remark",
        "personal_note",
        "special_title",
        "title",
        "wording",
    }
)
_SAFE_SCALAR_FIELDS = frozenset(
    {
        "approve",
        "at_sender",
        "ban",
        "ban_duration",
        "battery_status",
        "confirm_required",
        "count",
        "duration",
        "effectId",
        "effect_id",
        "emoji_id",
        "emoji_type",
        "emojiId",
        "emojiType",
        "enable",
        "ext_status",
        "face_id",
        "file_id",
        "folder",
        "folder_id",
        "group_id",
        "is_show_edit_card",
        "kick",
        "message_id",
        "new_parent_folder_id",
        "not_add",
        "pinned",
        "reject_add_request",
        "role",
        "robot_uin",
        "status",
        "sub_type",
        "target_id",
        "tip_window_type",
        "type",
        "user_id",
    }
)


def is_audited_side_effect(api: str) -> bool:
    if api.startswith("send_") and api not in _SIDE_EFFECT_EXACT:
        return False
    return api in _SIDE_EFFECT_EXACT or api.startswith(_SIDE_EFFECT_PREFIXES)


def _text(value: Any, limit: int) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set, bytes, bytearray)):
        return None
    normalized = str(value).strip()
    return normalized[:limit] if normalized else None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def safe_api_parameters(data: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in sorted(_SAFE_SCALAR_FIELDS):
        value = data.get(key)
        if isinstance(value, (str, int, float, bool)) and not (isinstance(value, str) and not value.strip()):
            safe[key] = value if not isinstance(value, str) else value[:512]
    for key in sorted(_SAFE_TEXT_FIELDS):
        value = _text(data.get(key), 4_096 if key in {"comment", "content"} else 512)
        if value is not None:
            safe[key] = value
    flag = _text(data.get("flag"), 4_096)
    if flag is not None:
        safe["flag_sha256"] = hashlib.sha256(flag.encode("utf-8")).hexdigest()
    for key in ("file", "image", "path", "url"):
        if data.get(key) is not None:
            safe[f"{key}_supplied"] = True
    operation = data.get("operation")
    if isinstance(operation, dict):
        for key in ("approve", "at_sender", "ban", "ban_duration", "delete", "kick"):
            value = operation.get(key)
            if isinstance(value, (int, float, bool)):
                safe[f"operation_{key}"] = value
        if operation.get("reply") is not None:
            safe["operation_reply_supplied"] = True
    return safe


def _conversation(data: dict[str, Any], bot_id: str) -> dict[str, str | None]:
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    group_id = _text(data.get("group_id") or context.get("group_id"), 256)
    if group_id is not None:
        return {"id": group_id, "type": "group", "name": None}
    user_id = _text(data.get("user_id") or data.get("target_id") or context.get("user_id"), 256)
    if user_id is not None:
        return {"id": user_id, "type": "private", "name": None}
    return {"id": bot_id, "type": "system", "name": None}


def _event_payload(
    *,
    instance: dict[str, Any],
    call_id: str,
    stage: str,
    conversation: dict[str, str | None],
    trigger_source_event_id: str | None,
    occurred_at: str,
    audit: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    source_event_id = f"qq:{instance['bot_id']}:api-call:{call_id}:{stage}"
    references = (
        [{"type": "derived_from", "source_event_id": trigger_source_event_id}]
        if trigger_source_event_id
        else []
    )
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "instance": instance,
        "event_type": f"audit.platform_api_call.{stage}",
        "conversation": conversation,
        "sender": None,
        "message": None,
        "references": references,
        "actions": [],
        "platform_api_call": audit,
        "occurred_at": occurred_at,
        "raw": None,
        "metadata": {"observation_method": "nonebot_onebot_api_hook"},
    }, hashlib.sha256(f"{instance['instance_id']}\x1f{source_event_id}".encode()).hexdigest()


def started_api_call(
    *,
    instance: dict[str, Any],
    api: str,
    data: dict[str, Any],
    trigger_source_event_id: str | None,
    occurred_at: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    call_id = str(uuid4())
    parameters = safe_api_parameters(data)
    conversation = _conversation(data, str(instance["bot_id"]))
    audit = {
        "call_id": call_id,
        "stage": "started",
        "api_name": api,
        "safe_parameters": parameters,
        "outcome": "pending",
        "success": None,
        "return_code": None,
        "duration_ms": None,
        "result_message_ids": [],
        "safe_error_code": None,
    }
    payload, key = _event_payload(
        instance=instance,
        call_id=call_id,
        stage="started",
        conversation=conversation,
        trigger_source_event_id=trigger_source_event_id,
        occurred_at=occurred_at,
        audit=audit,
    )
    return payload, key, {
        "instance": instance,
        "call_id": call_id,
        "api": api,
        "safe_parameters": parameters,
        "conversation": conversation,
        "trigger_source_event_id": trigger_source_event_id,
    }


def completed_api_call(
    context: dict[str, Any],
    *,
    exception: Exception | None,
    result: Any,
    duration_ms: int,
    occurred_at: str,
) -> tuple[dict[str, Any], str]:
    error_text = str(exception).lower() if exception is not None else ""
    outcome = (
        "succeeded"
        if exception is None
        else "ambiguous"
        if "timeout" in error_text or "timed out" in error_text
        else "failed"
    )
    result_dict = _mapping(result)
    return_code = result_dict.get("retcode")
    if isinstance(return_code, bool) or not isinstance(return_code, int):
        return_code = None
    message_ids: list[str] = []
    message_id = _text(result_dict.get("message_id"), 512)
    if message_id is not None:
        message_ids.append(message_id)
    raw_message_ids = result_dict.get("message_ids")
    if isinstance(raw_message_ids, list):
        message_ids.extend(
            value for item in raw_message_ids[:128] if (value := _text(item, 512)) is not None
        )
    message_ids = list(dict.fromkeys(message_ids))[:128]
    safe_error_code = None
    if exception is not None:
        safe_error_code = (
            "platform_completion_unknown"
            if outcome == "ambiguous"
            else f"platform_api_{type(exception).__name__.lower()}"[:128]
        )
    audit = {
        "call_id": context["call_id"],
        "stage": "completed",
        "api_name": context["api"],
        "safe_parameters": context["safe_parameters"],
        "outcome": outcome,
        "success": exception is None,
        "return_code": return_code,
        "duration_ms": max(0, min(duration_ms, 86_400_000)),
        "result_message_ids": message_ids,
        "safe_error_code": safe_error_code,
    }
    return _event_payload(
        instance=context["instance"],
        call_id=context["call_id"],
        stage="completed",
        conversation=context["conversation"],
        trigger_source_event_id=context["trigger_source_event_id"],
        occurred_at=occurred_at,
        audit=audit,
    )
