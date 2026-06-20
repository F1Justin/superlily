from typing import Any


CONVERSATION_TYPES = ("group", "private", "channel", "system")


def conversation(chat_key: str, chat_type: Any = None) -> dict[str, Any]:
    """Parse Nekro chat keys without leaking the type prefix into the ID."""

    adapter_prefix = "onebot_v11-"
    value = chat_key[len(adapter_prefix) :] if chat_key.startswith(adapter_prefix) else chat_key

    kind = str(getattr(chat_type, "value", chat_type) or "unknown")
    conversation_id = value
    for candidate in CONVERSATION_TYPES:
        for separator in ("_", "-"):
            prefix = f"{candidate}{separator}"
            if value.startswith(prefix):
                kind = candidate
                conversation_id = value[len(prefix) :]
                break
        else:
            continue
        break

    if kind not in CONVERSATION_TYPES:
        kind = "unknown"
    return {"id": conversation_id or "unknown", "type": kind, "name": None}


__all__ = ["conversation"]
