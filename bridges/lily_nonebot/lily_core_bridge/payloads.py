import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable


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


def _safe_platform_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.lower().startswith(("http://", "https://", "file://", "base64://", "data:")):
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


def source_event_id(event: Any, conversation: dict[str, Any], raw: dict[str, Any]) -> str:
    if message_id := getattr(event, "message_id", None):
        return f"qq:{conversation['type']}:{conversation['id']}:message:{message_id}"
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
