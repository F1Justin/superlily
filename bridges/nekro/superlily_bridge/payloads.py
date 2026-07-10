import json
from typing import Any


NATIVE_IDENTITY_SCHEMA = "onebot_v11.qq.native_identity.v1"
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


def _native_scalar(value: Any, *, max_length: int = 512) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    text = str(value).strip()
    if not text or text.lower().startswith(("http://", "https://", "file://", "base64://", "data:")):
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


def content_parts(items: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    segments: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    for item in items:
        data = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        data = json.loads(json.dumps(data, ensure_ascii=False, default=str))
        segments.append(data)
        item_type = str(data.get("type", "unknown"))
        if item_type in {"image", "file", "voice", "video"}:
            attachments.append(
                {
                    "type": item_type,
                    "name": data.get("file_name"),
                    "platform_id": None,
                    "size_bytes": None,
                }
            )
    return segments, attachments


def ref_msg_id_from_ext_data(ext_data: Any) -> str | None:
    if ext_data is None:
        return None
    if isinstance(ext_data, dict):
        ref_msg_id = ext_data.get("ref_msg_id")
    else:
        ref_msg_id = getattr(ext_data, "ref_msg_id", None)
    return str(ref_msg_id) if ref_msg_id else None


def message_references(
    segments: list[dict[str, Any]],
    conv: dict[str, Any],
    ref_msg_id: str | None = None,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(reply_id: Any, raw: dict[str, Any]) -> None:
        if reply_id is None:
            return
        normalized = str(reply_id)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        references.append(
            {
                "type": "reply_to",
                "platform_message_id": normalized,
                "conversation_id": conv["id"],
                "conversation_type": conv["type"],
                "raw": raw,
            }
        )

    for segment in segments:
        if segment.get("type") not in {"reply", "reference"}:
            continue
        data = segment.get("data", {}) or segment
        append(
            data.get("id") or data.get("message_id") or data.get("ref_msg_id"),
            {"segment": segment},
        )
    append(ref_msg_id, {"ext_data": {"ref_msg_id": ref_msg_id}})
    return references
