import time
from typing import Any


CONVERSATION_TYPES = ("group", "private", "channel", "system")


class NativeIdentityCache:
    """Small TTL cache joining a raw OneBot event to Nekro's normalized message."""

    def __init__(self, max_entries: int = 4096, ttl_seconds: float = 120.0) -> None:
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, tuple[float, dict[str, str]]] = {}

    def _prune(self, now: float) -> None:
        expired = [key for key, (created_at, _) in self._entries.items() if now - created_at > self.ttl_seconds]
        for key in expired:
            self._entries.pop(key, None)

    def put(self, key: str, identity: dict[str, str], *, now: float | None = None) -> None:
        if not key or not identity:
            return
        current = time.monotonic() if now is None else now
        self._prune(current)
        if key not in self._entries and len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item][0])
            self._entries.pop(oldest, None)
        self._entries[key] = (current, dict(identity))

    def pop(self, key: str, *, now: float | None = None) -> dict[str, str] | None:
        current = time.monotonic() if now is None else now
        self._prune(current)
        item = self._entries.pop(key, None)
        return None if item is None else item[1]


def native_identity_cache_key(conv: dict[str, Any], message_id: Any) -> str:
    return f"{conv.get('type', 'unknown')}:{conv.get('id', 'unknown')}:{message_id}"


def claim_targets_instance(claim: Any, instance_id: str) -> bool:
    """Return whether Core selected this bridge instance to handle the event."""

    return bool(
        isinstance(claim, dict)
        and claim.get("ready") is True
        and claim.get("action") == "allow"
        and claim.get("reason") == f"decision_target:{instance_id}"
    )


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


__all__ = [
    "NativeIdentityCache",
    "claim_targets_instance",
    "conversation",
    "native_identity_cache_key",
]
