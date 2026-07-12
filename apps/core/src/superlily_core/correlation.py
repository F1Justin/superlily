import hashlib
import json
from typing import Any

from superlily_contracts import EventIn


CORRELATION_VERSION = "qq-message-v3"


def canonical_conversation_id(platform: str, conversation_type: str, conversation_id: str) -> str:
    value = str(conversation_id)
    if platform == "qq":
        prefix = f"{conversation_type}_"
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


def canonical_event_type(platform: str, event_type: str) -> str:
    if platform == "qq" and event_type.split(".", 1)[0] == "message":
        return "message"
    return event_type


def _native_scalar(native_identity: dict[str, Any], field: str) -> str | None:
    value = native_identity.get(field)
    if value is None or isinstance(value, (dict, list, tuple, set, bytes, bytearray, bool)):
        return None
    normalized = str(value).strip()
    return normalized or None


def event_correlation_fingerprint(payload: EventIn) -> str | None:
    """Return a strong cross-account QQ identity fingerprint.

    NapCat message IDs are account-local.  Correlation v3 therefore requires
    the content-free native ``real_seq`` captured from the original OneBot
    event.  Missing or internally inconsistent identity stays uncorrelated;
    text and short time windows are never used as a fallback identity.
    """

    event_kind = canonical_event_type(payload.instance.platform, payload.event_type)
    native_identity = payload.metadata.get("native_identity")
    if (
        payload.instance.platform != "qq"
        or event_kind != "message"
        or payload.conversation.type != "group"
        or payload.sender is None
        or not isinstance(native_identity, dict)
    ):
        return None

    real_seq = _native_scalar(native_identity, "real_seq")
    if real_seq is None:
        return None

    conversation_id = canonical_conversation_id(
        payload.instance.platform,
        payload.conversation.type,
        payload.conversation.id,
    )

    native_sender = _native_scalar(native_identity, "user_id")
    if native_sender is not None and native_sender != str(payload.sender.id):
        return None
    native_group = _native_scalar(native_identity, "group_id")
    if native_group is not None:
        canonical_native_group = canonical_conversation_id("qq", "group", native_group)
        if canonical_native_group != conversation_id:
            return None

    material = [
        CORRELATION_VERSION,
        payload.instance.platform,
        event_kind,
        payload.conversation.type,
        conversation_id,
        payload.sender.id,
        real_seq,
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def advisory_lock_key(fingerprint: str) -> int:
    value = int(fingerprint[:16], 16)
    return value - (1 << 64) if value >= (1 << 63) else value
