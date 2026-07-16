import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable


NATIVE_IDENTITY_SCHEMA = "onebot_v11.qq.native_identity.v1"
MESSAGE_SOURCE_EVENT_ID_SCHEMA = "qq.source_event.v2"
_STRONG_NATIVE_IDENTITY_FIELDS = frozenset(
    {
        "message_seq",
        "real_id",
        "real_seq",
        "time",
        "msg_id",
        "msg_seq",
        "msg_random",
        "msg_uid",
        "peer_uid",
    }
)
_NATIVE_IDENTITY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("message_id", ("message_id",)),
    ("message_seq", ("message_seq",)),
    ("real_id", ("real_id",)),
    ("real_seq", ("real_seq",)),
    ("time", ("time",)),
    ("group_id", ("group_id",)),
    ("user_id", ("user_id",)),
    ("message_type", ("message_type",)),
    ("sub_type", ("sub_type",)),
    ("msg_id", ("msg_id", "msgId")),
    ("msg_seq", ("msg_seq", "msgSeq")),
    ("msg_random", ("msg_random", "msgRandom")),
    ("msg_uid", ("msg_uid", "msgUid")),
    ("peer_uid", ("peer_uid", "peerUid")),
    ("chat_type", ("chat_type", "chatType")),
)
_URI_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def utc_iso(timestamp: int | float | None = None) -> str:
    if timestamp is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def stable_key(*parts: Any) -> str:
    encoded = "\x1f".join(str(part) for part in parts).encode()
    return hashlib.sha256(encoded).hexdigest()


def model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def _native_scalar(value: Any, *, max_length: int = 512) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    text = str(value).strip()
    if not text or _URI_SCHEME.match(text):
        return None
    return text[:max_length]


def native_message_identity(*sources: Any) -> dict[str, str]:
    """Extract a strict, content-free allowlist of OneBot/NapCat identity fields."""

    values: dict[str, str] = {}
    for canonical, aliases in _NATIVE_IDENTITY_ALIASES:
        value: Any = None
        for source in sources:
            if source is None:
                continue
            for alias in aliases:
                if isinstance(source, dict):
                    candidate = source.get(alias)
                else:
                    candidate = getattr(source, alias, None)
                if candidate is not None:
                    value = candidate
                    break
            if value is not None:
                break
        normalized = _native_scalar(
            value,
            max_length=64 if canonical in {"message_type", "sub_type", "chat_type"} else 512,
        )
        if normalized is not None:
            values[canonical] = normalized
    if not values:
        return {}
    return {"schema": NATIVE_IDENTITY_SCHEMA, **values}


def message_source_event_id(
    conversation: dict[str, Any],
    platform_message_id: Any,
    native_identity: Any,
    *,
    sender_id: Any = None,
    occurred_at: Any = None,
) -> str:
    """Build a replay-stable, content-free local ID for a QQ message observation."""

    identity = native_message_identity(native_identity)
    identity_fields = {
        key: value
        for key, value in identity.items()
        if key != "schema"
    }
    material: dict[str, Any] = {
        "schema": MESSAGE_SOURCE_EVENT_ID_SCHEMA,
        "conversation": {
            "type": _native_scalar(conversation.get("type"), max_length=64),
            "id": _native_scalar(conversation.get("id")),
        },
        "platform_message_id": _native_scalar(platform_message_id),
        "native_identity": identity_fields,
    }
    if not _STRONG_NATIVE_IDENTITY_FIELDS.intersection(identity_fields):
        material["fallback"] = {
            "sender_id": _native_scalar(sender_id),
            "occurred_at": _native_scalar(occurred_at),
        }
    encoded = json.dumps(
        material,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"qq:source:v2:{digest}"


def _safe_platform_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if _URI_SCHEME.match(text):
        return None
    return text[:512]


def conversation_from_event(event: Any) -> dict[str, Any]:
    if group_id := getattr(event, "group_id", None):
        return {"id": str(group_id), "type": "group", "name": None}
    if user_id := getattr(event, "user_id", None):
        return {"id": str(user_id), "type": "private", "name": None}
    return {"id": "system", "type": "system", "name": None}


def conversation_from_api(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("group_id") is not None:
        return {"id": str(data["group_id"]), "type": "group", "name": None}
    target = data.get("user_id") or data.get("target_id") or "unknown"
    return {"id": str(target), "type": "private" if target != "unknown" else "unknown", "name": None}


def message_segments(message: Any) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    if message is None:
        return None, [], []
    if isinstance(message, str):
        return message, [{"type": "text", "data": {"text": message}}], []
    segments: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    texts: list[str] = []
    try:
        iterator: Iterable[Any] = iter(message)
    except TypeError:
        return str(message), [], []
    for segment in iterator:
        if isinstance(segment, dict):
            segment_type = str(segment.get("type", "unknown"))
            data = dict(segment.get("data", {}) or {})
        else:
            segment_type = str(getattr(segment, "type", "unknown"))
            data = dict(getattr(segment, "data", {}) or {})
        serializable = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        segments.append({"type": segment_type, "data": serializable})
        if segment_type == "text":
            texts.append(str(data.get("text", "")))
        if segment_type in {"image", "file", "record", "video"}:
            attachments.append(
                {
                    "type": segment_type,
                    "name": data.get("name") or data.get("file_name"),
                    "platform_id": _safe_platform_id(data.get("file")),
                    "size_bytes": data.get("file_size") if isinstance(data.get("file_size"), int) else None,
                }
            )
    text = "".join(texts) or None
    return text, segments, attachments


def event_message(event: Any) -> Any:
    return getattr(event, "original_message", None) or event.get_message()


def message_references(segments: list[dict[str, Any]], conversation: dict[str, Any]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for segment in segments:
        if segment.get("type") != "reply":
            continue
        data = segment.get("data", {}) or {}
        reply_id = data.get("id") or data.get("message_id")
        if reply_id is None:
            continue
        references.append(
            {
                "type": "reply_to",
                "platform_message_id": str(reply_id),
                "conversation_id": conversation["id"],
                "conversation_type": conversation["type"],
                "raw": {"segment": segment},
            }
        )
    return references


def source_event_id(event: Any, conversation: dict[str, Any], raw: dict[str, Any]) -> str:
    message_id = getattr(event, "message_id", None)
    if message_id is not None and str(message_id):
        return message_source_event_id(
            conversation,
            message_id,
            native_message_identity(raw, event),
            sender_id=getattr(event, "user_id", None) or raw.get("user_id"),
            occurred_at=getattr(event, "time", None) or raw.get("time"),
        )
    event_name = event.get_event_name() if hasattr(event, "get_event_name") else raw.get("post_type", "event")
    fingerprint = stable_key(
        event_name,
        raw.get("time"),
        raw.get("group_id"),
        raw.get("user_id"),
        raw.get("operator_id"),
        raw.get("notice_type"),
        raw.get("request_type"),
    )[:24]
    return f"qq:{conversation['type']}:{conversation['id']}:{event_name}:{fingerprint}"
