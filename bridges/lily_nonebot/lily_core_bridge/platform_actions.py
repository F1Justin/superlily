"""Honest, bounded OneBot v11/NapCat platform-action normalization."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse


ACTION_SANITIZER_VERSION = "onebot-v11-actions-v1"
ACTION_SOURCE_EVENT_ID_SCHEMA = "qq.action.source.v1"
_SUPPORTED_NOTICE_TYPES = frozenset(
    {"group_msg_emoji_like", "group_recall", "friend_recall"}
)
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
    if raw.get("post_type") != "notice":
        return False
    notice_type = raw.get("notice_type")
    return notice_type in _SUPPORTED_NOTICE_TYPES or (
        notice_type == "notify" and raw.get("sub_type") == "poke"
    )


def normalize_platform_action_event(
    raw: dict[str, Any],
    conversation: dict[str, Any],
) -> dict[str, Any] | None:
    """Return normalized actions and capture evidence for a supported notice."""

    if not is_supported_action_event(raw):
        return None

    notice_type = str(raw.get("notice_type"))
    target_conversation = _conversation(conversation)
    occurred_at = _occurred_at(raw.get("time"))
    event_reasons: list[str] = []
    omitted_fields: list[str] = []
    actions: list[dict[str, Any]] = []
    if occurred_at is None:
        event_reasons.append("platform event time missing; bridge capture time used")

    if notice_type == "group_msg_emoji_like":
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
    return {
        "schema_version": "1.0",
        "source_event_id": action_source_event_id(raw, conversation),
        "instance": instance,
        "event_type": event_type,
        "conversation": _conversation(conversation),
        "sender": (
            {"id": sender_id, "name": None, "roles": []}
            if sender_id is not None
            else None
        ),
        "message": None,
        "references": [],
        "capture": normalized["capture"],
        "actions": normalized["actions"],
        "occurred_at": _occurred_at(raw.get("time")) or fallback_occurred_at,
        "raw": None,
        "metadata": {"to_me": bool(to_me)},
    }
