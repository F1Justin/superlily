"""Honest, bounded OneBot v11/NapCat platform-action normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse


ACTION_SANITIZER_VERSION = "onebot-v11-actions-v2"
ACTION_SOURCE_EVENT_ID_SCHEMA = "qq.action.source.v1"
_SUPPORTED_NOTICE_TYPES = frozenset(
    {
        "bot_offline",
        "essence",
        "friend_add",
        "friend_recall",
        "group_admin",
        "group_ban",
        "group_card",
        "group_decrease",
        "group_increase",
        "group_msg_emoji_like",
        "group_recall",
        "group_upload",
    }
)
_SUPPORTED_NOTIFY_SUBTYPES = frozenset({"group_name", "poke", "title"})
_SUPPORTED_REQUEST_TYPES = frozenset({"friend", "group"})
_COMMON_FIELDS = frozenset(
    {
        "time",
        "self_id",
        "post_type",
        "notice_type",
        "sub_type",
        "group_id",
        "user_id",
        "operator_id",
        "target_id",
        "message_id",
        "likes",
        "raw_info",
        "card_new",
        "card_old",
        "name_new",
        "title",
        "duration",
        "sender_id",
        "file",
        "comment",
        "flag",
        "request_type",
        "tag",
        "message",
    }
)


def _text(value: Any, *, max_length: int) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    result = str(value).strip()
    return result[:max_length] if result else None


def _principal(value: Any) -> str | None:
    return _text(value, max_length=256)


def _platform_message_id(value: Any) -> str | None:
    return _text(value, max_length=512)


def _state_text(value: Any, *, max_length: int) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    return str(value)[:max_length]


def _occurred_at(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    except (OverflowError, TypeError, ValueError):
        return None


def _conversation(value: dict[str, Any]) -> dict[str, Any]:
    conversation_type = str(value.get("type") or "unknown")
    if conversation_type not in {"group", "private", "channel", "system", "unknown"}:
        conversation_type = "unknown"
    return {
        "id": _text(value.get("id"), max_length=256) or "unknown",
        "type": conversation_type,
        "name": _text(value.get("name"), max_length=512),
    }


def _payload_evidence(raw: dict[str, Any]) -> tuple[str, int]:
    encoded = json.dumps(
        raw,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _unknown_fields(raw: dict[str, Any]) -> list[str]:
    return sorted(
        f"raw.{str(key)[:128]}"
        for key in raw
        if key not in _COMMON_FIELDS
    )[:256]


def _reason(parts: list[str]) -> str | None:
    return "; ".join(dict.fromkeys(parts)) or None


def _capture(
    raw: dict[str, Any],
    status: str,
    reasons: list[str],
    omitted_fields: list[str] | None = None,
) -> dict[str, Any]:
    payload_sha256, payload_size = _payload_evidence(raw)
    return {
        "schema_version": "1.0",
        "status": status,
        "sanitizer_version": ACTION_SANITIZER_VERSION,
        "original_payload_sha256": payload_sha256,
        "original_payload_size_bytes": payload_size,
        "omitted_fields": sorted(
            set(_unknown_fields(raw) + (omitted_fields or []))
        )[:256],
        "platform_extra": {},
        "reason": _reason(reasons),
    }


def _bounded_count(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return value


def _poke_value(raw_info: Any) -> tuple[dict[str, Any], list[str], list[str]]:
    if not isinstance(raw_info, list):
        return {"sub_type": "poke"}, [], []

    texts: list[str] = []
    action_id: str | None = None
    effect_id: str | None = None
    omitted: list[str] = []
    reasons: list[str] = []
    for item in raw_info[:64]:
        if not isinstance(item, dict):
            reasons.append("raw_info contains a non-object item")
            continue
        item_type = _text(item.get("type"), max_length=32)
        text = _text(item.get("txt"), max_length=1_024)
        if item_type == "nor" and text:
            texts.append(text)
        jump_url = item.get("jp")
        if isinstance(jump_url, str):
            try:
                query = parse_qs(urlparse(jump_url).query)
                action_id = action_id or _text(
                    (query.get("actionId") or [None])[0], max_length=32
                )
                effect_id = effect_id or _text(
                    (query.get("effectId") or [None])[0], max_length=32
                )
            except ValueError:
                reasons.append("raw_info jump URL could not be parsed")
        for field in ("jp", "src", "uid"):
            if item.get(field) is not None:
                omitted.append(f"raw.raw_info[*].{field}")

    display_text = "".join(texts)
    if len(display_text) > 1_024:
        display_text = display_text[:1_024]
        reasons.append("poke display text was truncated to 1024 characters")
        omitted.append("raw.raw_info[*].txt[1024:]")
    if len(raw_info) > 64:
        reasons.append("raw_info was truncated to 64 items")
        omitted.append("raw.raw_info[64:]")

    value: dict[str, Any] = {"sub_type": "poke"}
    if display_text:
        value["display_text"] = display_text
    if action_id:
        value["action_id"] = action_id
    if effect_id:
        value["effect_id"] = effect_id
    return value, omitted, reasons


def is_supported_action_event(raw: dict[str, Any]) -> bool:
    if raw.get("post_type") == "request":
        return raw.get("request_type") in _SUPPORTED_REQUEST_TYPES
    if raw.get("post_type") != "notice":
        return False
    notice_type = raw.get("notice_type")
    return notice_type in _SUPPORTED_NOTICE_TYPES or (
        notice_type == "notify" and raw.get("sub_type") in _SUPPORTED_NOTIFY_SUBTYPES
    )


def normalize_platform_action_event(
    raw: dict[str, Any],
    conversation: dict[str, Any],
) -> dict[str, Any] | None:
    """Return normalized actions and capture evidence for a supported notice."""

    if not is_supported_action_event(raw):
        return None

    post_type = str(raw.get("post_type"))
    notice_type = str(raw.get("notice_type"))
    target_conversation = _conversation(conversation)
    occurred_at = _occurred_at(raw.get("time"))
    event_reasons: list[str] = []
    omitted_fields: list[str] = []
    actions: list[dict[str, Any]] = []
    if occurred_at is None:
        event_reasons.append("platform event time missing; bridge capture time used")

    if post_type == "request":
        request_type = str(raw.get("request_type"))
        subject = _principal(raw.get("user_id"))
        missing: list[str] = []
        value: dict[str, Any] = {"request_type": request_type}
        if request_type == "group":
            subtype = _text(raw.get("sub_type"), max_length=64)
            if subtype is None:
                missing.append("group request sub_type missing")
            else:
                value["sub_type"] = subtype
        comment = _state_text(raw.get("comment"), max_length=4_096)
        flag = _state_text(raw.get("flag"), max_length=1_024)
        if comment is not None:
            value["comment"] = comment
        if flag is None:
            missing.append("request flag missing")
        else:
            value["flag"] = flag
        if subject is None:
            missing.append("request subject user_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("request has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": f"{request_type}_request",
                "operation": "observed_state",
                "actor_principal_id": None,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "group_msg_emoji_like":
        actor = _principal(raw.get("user_id"))
        target_message = _platform_message_id(raw.get("message_id"))
        if target_message is None:
            event_reasons.append("reaction target message_id missing; action omitted")
            return {
                "actions": [],
                "capture": _capture(
                    raw,
                    "unavailable",
                    event_reasons,
                    omitted_fields,
                ),
            }

        likes = raw.get("likes")
        if not isinstance(likes, list) or not likes:
            missing = ["reaction likes value missing"]
            if actor is None:
                missing.append("reaction actor user_id missing")
            if occurred_at is None:
                missing.append("platform event time missing")
            actions.append(
                {
                    "schema_version": "1.0",
                    "action_kind": "reaction",
                    "operation": "observed_state",
                    "actor_principal_id": actor,
                    "subject_principal_id": None,
                    "target_source_event_id": None,
                    "target_platform_message_id": target_message,
                    "target_conversation": target_conversation,
                    "value": {},
                    "capture_status": "partial",
                    "occurred_at": occurred_at,
                    "reason": _reason(missing),
                }
            )
            event_reasons.extend(missing)
        else:
            for like in likes[:128]:
                missing: list[str] = []
                value: dict[str, Any] = {}
                if isinstance(like, dict):
                    emoji_id = _text(like.get("emoji_id"), max_length=128)
                    count = _bounded_count(like.get("count"))
                    if emoji_id is None:
                        missing.append("reaction emoji_id missing")
                    else:
                        value["emoji_id"] = emoji_id
                    if count is None:
                        missing.append("reaction count missing or invalid")
                    else:
                        value["count"] = count
                else:
                    missing.append("reaction likes item is not an object")
                if actor is None:
                    missing.append("reaction actor user_id missing")
                if occurred_at is None:
                    missing.append("platform event time missing")
                actions.append(
                    {
                        "schema_version": "1.0",
                        "action_kind": "reaction",
                        "operation": "observed_state",
                        "actor_principal_id": actor,
                        "subject_principal_id": None,
                        "target_source_event_id": None,
                        "target_platform_message_id": target_message,
                        "target_conversation": target_conversation,
                        "value": value,
                        "capture_status": "partial" if missing else "complete",
                        "occurred_at": occurred_at,
                        "reason": _reason(missing),
                    }
                )
                event_reasons.extend(missing)
            if len(likes) > 128:
                event_reasons.append("reaction likes truncated to 128 items")
                omitted_fields.append("raw.likes[128:]")

    elif notice_type == "group_card":
        subject = _principal(raw.get("user_id"))
        card_new = _state_text(raw.get("card_new"), max_length=512)
        card_old = _state_text(raw.get("card_old"), max_length=512)
        missing = []
        if subject is None:
            missing.append("group-card subject user_id missing")
        if card_new is None:
            missing.append("group-card card_new missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("group-card change has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        value = {}
        if card_old is not None:
            value["card_old"] = card_old
        if card_new is not None:
            value["card_new"] = card_new
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_card",
                "operation": "update",
                "actor_principal_id": subject,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "notify" and raw.get("sub_type") == "group_name":
        actor = _principal(raw.get("user_id"))
        group_id = _principal(raw.get("group_id"))
        group_subject = f"qq:group:{group_id}" if group_id is not None else None
        name_new = _state_text(raw.get("name_new"), max_length=512)
        missing = []
        if group_subject is None:
            missing.append("group-name group_id missing")
        if name_new is None or not name_new.strip():
            missing.append("group-name name_new missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if group_subject is None:
            event_reasons.extend(missing)
            event_reasons.append("group-name change has no group subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_name",
                "operation": "update",
                "actor_principal_id": actor,
                "subject_principal_id": group_subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": {"name_new": name_new} if name_new is not None else {},
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type in {"group_increase", "group_decrease"}:
        subject = _principal(raw.get("user_id"))
        actor = _principal(raw.get("operator_id"))
        subtype = _text(raw.get("sub_type"), max_length=64)
        missing = []
        if subject is None:
            missing.append("membership subject user_id missing")
        if subtype is None:
            missing.append("membership sub_type missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("membership change has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_membership",
                "operation": "add" if notice_type == "group_increase" else "remove",
                "actor_principal_id": actor,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": {"sub_type": subtype} if subtype is not None else {},
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "group_admin":
        subject = _principal(raw.get("user_id"))
        subtype = _text(raw.get("sub_type"), max_length=64)
        missing = []
        if subject is None:
            missing.append("group-admin subject user_id missing")
        if subtype not in {"set", "unset"}:
            missing.append("group-admin sub_type missing or invalid")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("group-admin change has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        value = {"role": "admin"}
        if subtype in {"set", "unset"}:
            value["active"] = subtype == "set"
            value["sub_type"] = subtype
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_role",
                "operation": "update",
                "actor_principal_id": None,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "group_ban":
        actor = _principal(raw.get("operator_id"))
        subject = _principal(raw.get("user_id"))
        subtype = _text(raw.get("sub_type"), max_length=64)
        duration = _bounded_count(raw.get("duration"))
        missing = []
        if actor is None:
            missing.append("group-ban operator_id missing")
        if subject is None:
            missing.append("group-ban subject user_id missing")
        if subtype is None:
            missing.append("group-ban sub_type missing")
        if duration is None:
            missing.append("group-ban duration missing or invalid")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("group-ban change has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        value = {}
        if subtype is not None:
            value["sub_type"] = subtype
        if duration is not None:
            value["duration_seconds"] = duration
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_ban",
                "operation": "update",
                "actor_principal_id": actor,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "notify" and raw.get("sub_type") == "title":
        subject = _principal(raw.get("user_id"))
        title = _state_text(raw.get("title"), max_length=512)
        missing = []
        if subject is None:
            missing.append("group-title subject user_id missing")
        if title is None:
            missing.append("group-title value missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("group-title change has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_title",
                "operation": "update",
                "actor_principal_id": None,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": {"title": title} if title is not None else {},
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "essence":
        actor = _principal(raw.get("operator_id"))
        subject = _principal(raw.get("sender_id"))
        target_message = _platform_message_id(raw.get("message_id"))
        subtype = _text(raw.get("sub_type"), max_length=64)
        missing = []
        if actor is None:
            missing.append("essence operator_id missing")
        if subject is None:
            missing.append("essence sender_id missing")
        if target_message is None:
            missing.append("essence message_id missing")
        if subtype is None:
            missing.append("essence sub_type missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None and target_message is None:
            event_reasons.extend(missing)
            event_reasons.append("essence change has no target hint; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "essence",
                "operation": "remove" if subtype in {"delete", "remove"} else "add",
                "actor_principal_id": actor,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": target_message,
                "target_conversation": target_conversation,
                "value": {"sub_type": subtype} if subtype is not None else {},
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "group_upload":
        actor = _principal(raw.get("user_id"))
        file_info = raw.get("file")
        missing = []
        value: dict[str, Any] = {}
        if not isinstance(file_info, dict):
            missing.append("group-upload file object missing")
        else:
            aliases = {
                "file_id": ("id", "file_id"),
                "name": ("name", "file_name"),
                "size_bytes": ("size", "file_size"),
                "busid": ("busid",),
            }
            for output, inputs in aliases.items():
                candidate = next(
                    (
                        file_info.get(key)
                        for key in inputs
                        if file_info.get(key) is not None
                    ),
                    None,
                )
                if output == "size_bytes":
                    normalized_value = _bounded_count(candidate)
                else:
                    normalized_value = _state_text(candidate, max_length=512)
                if normalized_value is None and output in {"file_id", "name", "size_bytes"}:
                    missing.append(f"group-upload {output} missing")
                elif normalized_value is not None:
                    value[output] = normalized_value
            known_file_fields = {field for fields in aliases.values() for field in fields}
            omitted_fields.extend(
                f"raw.file.{str(key)[:128]}"
                for key in file_info
                if key not in known_file_fields
            )
        if actor is None:
            missing.append("group-upload uploader user_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if actor is None:
            event_reasons.extend(missing)
            event_reasons.append("group-upload has no uploader subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons, omitted_fields),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "group_file",
                "operation": "add",
                "actor_principal_id": actor,
                "subject_principal_id": actor,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "friend_add":
        subject = _principal(raw.get("user_id"))
        missing = []
        if subject is None:
            missing.append("friend-add user_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("friend-add has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "friendship",
                "operation": "add",
                "actor_principal_id": subject,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": {},
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type == "bot_offline":
        subject = _principal(raw.get("user_id"))
        tag = _state_text(raw.get("tag"), max_length=256)
        message = _state_text(raw.get("message"), max_length=4_096)
        missing = []
        if subject is None:
            missing.append("bot-offline user_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("bot-offline has no subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(raw, "unavailable", event_reasons),
            }
        value = {"online": False}
        if tag is not None:
            value["tag"] = tag
        if message is not None:
            value["message"] = message
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "bot_status",
                "operation": "observed_state",
                "actor_principal_id": None,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    elif notice_type in {"group_recall", "friend_recall"}:
        subject = _principal(raw.get("user_id"))
        actor = (
            _principal(raw.get("operator_id"))
            if notice_type == "group_recall"
            else subject
        )
        target_message = _platform_message_id(raw.get("message_id"))
        missing = []
        if actor is None:
            missing.append("recall actor missing")
        if subject is None:
            missing.append("recall subject user_id missing")
        if target_message is None:
            missing.append("recall target message_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if target_message is None and subject is None:
            event_reasons.extend(missing)
            event_reasons.append("recall has no target hint; action omitted")
            return {
                "actions": [],
                "capture": _capture(
                    raw,
                    "unavailable",
                    event_reasons,
                    omitted_fields,
                ),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "recall",
                "operation": "remove",
                "actor_principal_id": actor,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": target_message,
                "target_conversation": target_conversation,
                "value": {
                    "scope": "group" if notice_type == "group_recall" else "private"
                },
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    else:
        actor = _principal(raw.get("user_id"))
        subject = _principal(raw.get("target_id"))
        value, poke_omitted, poke_reasons = _poke_value(raw.get("raw_info"))
        omitted_fields.extend(poke_omitted)
        missing = list(poke_reasons)
        if actor is None:
            missing.append("poke actor user_id missing")
        if subject is None:
            missing.append("poke target_id missing")
        if occurred_at is None:
            missing.append("platform event time missing")
        if subject is None:
            event_reasons.extend(missing)
            event_reasons.append("poke has no target subject; action omitted")
            return {
                "actions": [],
                "capture": _capture(
                    raw,
                    "unavailable",
                    event_reasons,
                    omitted_fields,
                ),
            }
        actions.append(
            {
                "schema_version": "1.0",
                "action_kind": "poke",
                "operation": "observed_state",
                "actor_principal_id": actor,
                "subject_principal_id": subject,
                "target_source_event_id": None,
                "target_platform_message_id": None,
                "target_conversation": target_conversation,
                "value": value,
                "capture_status": "partial" if missing else "complete",
                "occurred_at": occurred_at,
                "reason": _reason(missing),
            }
        )
        event_reasons.extend(missing)

    status = "partial" if event_reasons else "complete"
    return {
        "actions": actions,
        "capture": _capture(raw, status, event_reasons, omitted_fields),
    }


def action_source_event_id(raw: dict[str, Any], conversation: dict[str, Any]) -> str:
    """Build a replay-stable identity from action facts, excluding observer self_id."""

    normalized = normalize_platform_action_event(raw, conversation)
    material = {
        "schema": ACTION_SOURCE_EVENT_ID_SCHEMA,
        "conversation": _conversation(conversation),
        "notice_type": _text(raw.get("notice_type"), max_length=64),
        "sub_type": _text(raw.get("sub_type"), max_length=64),
        "time": _text(raw.get("time"), max_length=64),
        "user_id": _principal(raw.get("user_id")),
        "operator_id": _principal(raw.get("operator_id")),
        "target_id": _principal(raw.get("target_id")),
        "message_id": _platform_message_id(raw.get("message_id")),
        "actions": normalized["actions"] if normalized else [],
    }
    encoded = json.dumps(
        material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"qq:action:v1:{hashlib.sha256(encoded).hexdigest()}"


def platform_action_event_payload(
    raw: dict[str, Any],
    conversation: dict[str, Any],
    instance: dict[str, Any],
    *,
    event_type: str,
    fallback_occurred_at: str,
    to_me: bool = False,
) -> dict[str, Any] | None:
    """Build the shared full event envelope used by both production bridges."""

    normalized = normalize_platform_action_event(raw, conversation)
    if normalized is None:
        return None
    sender_id = _principal(raw.get("user_id"))
    action_conversation = _conversation(conversation)
    sender: dict[str, Any] | None = None
    if sender_id is not None:
        sender = {"id": sender_id, "name": None, "roles": []}
        if raw.get("notice_type") == "group_card":
            card_new = _state_text(raw.get("card_new"), max_length=512)
            sender.update(
                {
                    "display_name": card_new,
                    "name": card_new,
                }
            )
    if raw.get("notice_type") == "notify" and raw.get("sub_type") == "group_name":
        action_conversation["name"] = _state_text(raw.get("name_new"), max_length=512)
    return {
        "schema_version": "1.0",
        "source_event_id": action_source_event_id(raw, conversation),
        "instance": instance,
        "event_type": event_type,
        "conversation": action_conversation,
        "sender": sender,
        "message": None,
        "references": [],
        "capture": normalized["capture"],
        "actions": normalized["actions"],
        "occurred_at": _occurred_at(raw.get("time")) or fallback_occurred_at,
        "raw": None,
        "metadata": {"to_me": bool(to_me)},
    }
