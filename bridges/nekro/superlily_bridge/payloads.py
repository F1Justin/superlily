import json
from typing import Any


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
