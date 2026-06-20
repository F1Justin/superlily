import hashlib
import json
import re
import unicodedata

from superlily_contracts import EventIn


CORRELATION_VERSION = "qq-text-v1"
_WHITESPACE = re.compile(r"\s+")


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


def normalized_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _WHITESPACE.sub(" ", unicodedata.normalize("NFC", value).strip())
    return normalized or None


def event_correlation_fingerprint(payload: EventIn) -> str | None:
    """Return a conservative fingerprint for cross-account text correlation."""

    text = normalized_text(payload.message.text if payload.message else None)
    event_kind = canonical_event_type(payload.instance.platform, payload.event_type)
    if (
        payload.instance.platform != "qq"
        or event_kind != "message"
        or payload.sender is None
        or text is None
    ):
        return None
    conversation_id = canonical_conversation_id(
        payload.instance.platform,
        payload.conversation.type,
        payload.conversation.id,
    )
    material = [
        CORRELATION_VERSION,
        payload.instance.platform,
        event_kind,
        payload.conversation.type,
        conversation_id,
        payload.sender.id,
        text,
    ]
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def advisory_lock_key(fingerprint: str) -> int:
    value = int(fingerprint[:16], 16)
    return value - (1 << 64) if value >= (1 << 63) else value
