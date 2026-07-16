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


class _ResponseTriggerState:
    __slots__ = (
        "current_source",
        "current_task_token",
        "next_source",
        "pending_source",
        "updated_at",
    )

    def __init__(self, updated_at: float) -> None:
        self.current_task_token: Any | None = None
        self.current_source: str | None = None
        self.next_source: str | None = None
        self.pending_source: str | None = None
        self.updated_at = updated_at


class ResponseTriggerTracker:
    """Track response attribution across Nekro's per-chat task scheduler.

    Nekro debounces messages that arrive before a task starts and queues the
    latest message that arrives while a task is running.  A single
    conversation-level source slot therefore loses attribution as soon as a
    second message arrives before the first task emits its response.  This
    tracker mirrors those scheduler states without importing Nekro itself:

    * ``next_source`` is the latest trigger observed while no task exists;
    * ``pending_source`` is the latest trigger observed during the active task;
    * ``current_source`` remains attached to one task token for all its output.

    ``task_token`` should be a stable identity for the current task, normally
    ``id(message_service.running_tasks[chat_key])``.  A response from a task
    first observed without a trigger establishes a source-less system task;
    messages received during it remain pending until the task token changes.
    """

    def __init__(self, max_entries: int = 4096, ttl_seconds: float = 180.0) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._states: dict[tuple[str, str], _ResponseTriggerState] = {}

    @staticmethod
    def _key(conv: dict[str, Any]) -> tuple[str, str]:
        return (str(conv.get("type", "unknown")), str(conv.get("id", "unknown")))

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, state in self._states.items()
            if now - state.updated_at > self.ttl_seconds
        ]
        for key in expired:
            self._states.pop(key, None)

    def _state(
        self,
        conv: dict[str, Any],
        *,
        now: float,
        create: bool,
    ) -> _ResponseTriggerState | None:
        self._prune(now)
        key = self._key(conv)
        state = self._states.get(key)
        if state is not None:
            state.updated_at = now
            return state
        if not create:
            return None
        if len(self._states) >= self.max_entries:
            oldest = min(self._states, key=lambda item: self._states[item].updated_at)
            self._states.pop(oldest, None)
        state = _ResponseTriggerState(now)
        self._states[key] = state
        return state

    @staticmethod
    def _transition_task(
        state: _ResponseTriggerState,
        task_token: Any,
    ) -> None:
        if state.current_task_token == task_token:
            return
        if state.current_task_token is None:
            # A trigger recorded while idle belongs to the first task that
            # follows it.  With no such trigger, this is a source-less system
            # task and current_source intentionally remains None.
            state.current_source = state.next_source
            state.next_source = None
        else:
            # Messages observed during the previous task belong to the next
            # task.  Do not consume this value while the old token keeps
            # producing output.
            state.current_source = state.pending_source
            state.pending_source = None
        state.current_task_token = task_token

    def remember(
        self,
        conv: dict[str, Any],
        source: str,
        task_token: Any | None,
        *,
        now: float | None = None,
    ) -> None:
        """Remember a trigger using the scheduler state visible on receipt."""

        if not source:
            return
        current = time.monotonic() if now is None else now
        state = self._state(conv, now=current, create=True)
        assert state is not None

        if task_token is None:
            # An explicit idle observation closes the prior task.  A pending
            # trigger, if any, becomes the next debounced trigger before the
            # newly observed source replaces it.
            state.current_task_token = None
            state.current_source = None
            if state.pending_source is not None:
                state.next_source = state.pending_source
                state.pending_source = None
            if state.next_source != source:
                state.next_source = source
            return

        if state.current_task_token is None:
            # The active token may first become visible when another message
            # arrives, before the original task has emitted any response.  Its
            # idle/debounced source must become current before the new source
            # is assigned to pending.
            state.current_task_token = task_token
            state.current_source = state.next_source
            state.next_source = None
            if state.current_source == source:
                return
        elif state.current_task_token != task_token:
            self._transition_task(state, task_token)

        if source in {state.current_source, state.pending_source}:
            return
        state.pending_source = source

    def observe_task(
        self,
        conv: dict[str, Any],
        task_token: Any | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """Observe a scheduler task transition and return its bound source."""

        current = time.monotonic() if now is None else now
        state = self._state(conv, now=current, create=task_token is not None)
        if state is None:
            return None
        if task_token is None:
            state.current_task_token = None
            state.current_source = None
            if state.pending_source is not None:
                state.next_source = state.pending_source
                state.pending_source = None
            return None
        self._transition_task(state, task_token)
        return state.current_source

    def source_for_response(
        self,
        conv: dict[str, Any],
        task_token: Any | None,
        *,
        now: float | None = None,
    ) -> str | None:
        """Return, without consuming, the source bound to the response task."""

        if task_token is None:
            return None
        return self.observe_task(conv, task_token, now=now)

    def forget(
        self,
        conv: dict[str, Any],
        source: str,
        *,
        now: float | None = None,
    ) -> None:
        """Remove exactly one denied source without disturbing its neighbours."""

        if not source:
            return
        current = time.monotonic() if now is None else now
        state = self._state(conv, now=current, create=False)
        if state is None:
            return
        if state.current_source == source:
            state.current_source = None
        if state.next_source == source:
            state.next_source = None
        if state.pending_source == source:
            state.pending_source = None

        if (
            state.current_task_token is None
            and state.current_source is None
            and state.next_source is None
            and state.pending_source is None
        ):
            self._states.pop(self._key(conv), None)

    def __len__(self) -> int:
        return len(self._states)


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


def claim_decision_targets_instance(claim: Any, instance_id: str) -> bool:
    """Return whether the canonical decision selected this bridge instance.

    This remains true when an otherwise actionable target claim safely
    degrades to ``abstain`` while waiting for peer-deny coordination.  The
    bridge uses it only to associate a later legacy response with its trigger;
    it does not treat the claim as authorization.
    """

    if not isinstance(claim, dict):
        return False
    features = claim.get("features")
    gates = features.get("gates") if isinstance(features, dict) else None
    return bool(
        isinstance(gates, dict)
        and gates.get("decision_type") in {"command", "talk"}
        and gates.get("target_instance_id") == instance_id
        and claim.get("action") != "deny"
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
    "ResponseTriggerTracker",
    "claim_decision_targets_instance",
    "claim_targets_instance",
    "conversation",
    "native_identity_cache_key",
]
