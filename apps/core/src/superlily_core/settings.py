import json
import os
from dataclasses import dataclass, field

from .command_registry import DEFAULT_COMMAND_REGISTRY_PATH

DEFAULT_DATABASE_URL = "postgresql+asyncpg://superlily:superlily@127.0.0.1:5432/superlily"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _string_set(value: str | None, *, variable: str) -> frozenset[str]:
    if not value:
        return frozenset()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be a JSON array of strings") from exc
    if not isinstance(parsed, list) or not all(isinstance(item, str) and item.strip() for item in parsed):
        raise ValueError(f"{variable} must be a JSON array of non-empty strings")
    return frozenset(item.strip() for item in parsed)


def _group_modes(value: str | None, *, variable: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be a JSON object of group keys to modes") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{variable} must be a JSON object of group keys to modes")
    modes: dict[str, str] = {}
    for raw_key, raw_mode in parsed.items():
        if not isinstance(raw_key, str) or not raw_key.strip() or not isinstance(raw_mode, str):
            raise ValueError(f"{variable} must map non-empty string keys to string modes")
        key = raw_key.strip()
        key_parts = key.split(":", 2)
        if len(key_parts) != 3 or not key_parts[0] or key_parts[1] != "group" or not key_parts[2]:
            raise ValueError(f"{variable} keys must use platform:group:id format")
        mode = raw_mode.strip().lower()
        if mode not in {"command_only", "conversation_only", "full", "observe_only"}:
            raise ValueError(
                f"{variable} modes must be command_only, conversation_only, full, or observe_only"
            )
        modes[key] = mode
    return modes


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    admin_token: str = ""
    ingest_tokens: dict[str, str] = field(default_factory=dict)
    stale_after_seconds: int = 90
    correlation_window_seconds: int = 2
    raw_enabled: bool = False
    raw_max_bytes: int = 32_768
    command_registry_path: str = DEFAULT_COMMAND_REGISTRY_PATH
    command_registry_snapshot_stale_seconds: int = 600
    group_default_mode: str = "command_only"
    group_modes: dict[str, str] = field(default_factory=dict)
    claim_mode: str = "off"
    claim_canary_conversations: frozenset[str] = field(default_factory=frozenset)
    claim_minimum_confidence: int = 85
    claim_required_observations: int = 2
    claim_coalesce_milliseconds: int = 200

    def __post_init__(self) -> None:
        active_ingest_tokens = [token for token in self.ingest_tokens.values() if token]
        if len(active_ingest_tokens) != len(set(active_ingest_tokens)):
            raise ValueError("ingest tokens must be unique per instance")
        if self.admin_token and self.admin_token in active_ingest_tokens:
            raise ValueError("admin and ingest tokens must be unrelated")
        if not 1 <= self.stale_after_seconds <= 86_400:
            raise ValueError("stale_after_seconds must be between 1 and 86400")
        if not 0 <= self.correlation_window_seconds <= 60:
            raise ValueError("correlation_window_seconds must be between 0 and 60")
        if not 1_024 <= self.raw_max_bytes <= 1_048_576:
            raise ValueError("raw_max_bytes must be between 1024 and 1048576")
        if self.claim_mode not in {"off", "shadow", "canary", "enforce"}:
            raise ValueError("claim_mode must be off, shadow, canary, or enforce")
        if not 0 <= self.claim_minimum_confidence <= 100:
            raise ValueError("claim_minimum_confidence must be between 0 and 100")
        if not 1 <= self.claim_required_observations <= 16:
            raise ValueError("claim_required_observations must be between 1 and 16")
        if not 0 <= self.claim_coalesce_milliseconds <= 5_000:
            raise ValueError("claim_coalesce_milliseconds must be between 0 and 5000")
        if self.command_registry_snapshot_stale_seconds < 1:
            raise ValueError("command_registry_snapshot_stale_seconds must be positive")
        valid_group_modes = {"command_only", "conversation_only", "full", "observe_only"}
        if self.group_default_mode not in valid_group_modes:
            raise ValueError(
                "group_default_mode must be command_only, conversation_only, full, or observe_only"
            )
        if any(mode not in valid_group_modes for mode in self.group_modes.values()):
            raise ValueError(
                "group_modes values must be command_only, conversation_only, full, or observe_only"
            )

    def conversation_mode(
        self,
        platform: str,
        conversation_type: str,
        canonical_conversation_id: str,
    ) -> str:
        if conversation_type != "group":
            return "full"
        key = f"{platform}:{conversation_type}:{canonical_conversation_id}"
        return self.group_modes.get(key, self.group_default_mode)

    @classmethod
    def from_env(cls) -> "Settings":
        tokens_raw = os.getenv("SUPERLILY_INGEST_TOKENS_JSON", "{}")
        try:
            tokens = json.loads(tokens_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SUPERLILY_INGEST_TOKENS_JSON must be valid JSON") from exc
        if not isinstance(tokens, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tokens.items()):
            raise ValueError("SUPERLILY_INGEST_TOKENS_JSON must be an object of string tokens")
        claim_mode = os.getenv("SUPERLILY_CLAIM_MODE", "off").strip().lower()
        if claim_mode not in {"off", "shadow", "canary", "enforce"}:
            raise ValueError("SUPERLILY_CLAIM_MODE must be off, shadow, canary, or enforce")
        return cls(
            database_url=os.getenv("SUPERLILY_DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_token=os.getenv("SUPERLILY_ADMIN_TOKEN", ""),
            ingest_tokens=tokens,
            stale_after_seconds=int(os.getenv("SUPERLILY_STALE_AFTER_SECONDS", "90")),
            correlation_window_seconds=int(os.getenv("SUPERLILY_CORRELATION_WINDOW_SECONDS", "2")),
            raw_enabled=_as_bool(os.getenv("SUPERLILY_RAW_ENABLED")),
            raw_max_bytes=int(os.getenv("SUPERLILY_RAW_MAX_BYTES", "32768")),
            command_registry_path=os.getenv("SUPERLILY_COMMAND_REGISTRY_PATH", DEFAULT_COMMAND_REGISTRY_PATH),
            command_registry_snapshot_stale_seconds=int(
                os.getenv("SUPERLILY_COMMAND_REGISTRY_SNAPSHOT_STALE_SECONDS", "600")
            ),
            group_default_mode=os.getenv("SUPERLILY_GROUP_DEFAULT_MODE", "command_only").strip().lower(),
            group_modes=_group_modes(
                os.getenv("SUPERLILY_GROUP_MODES_JSON"),
                variable="SUPERLILY_GROUP_MODES_JSON",
            ),
            claim_mode=claim_mode,
            claim_canary_conversations=_string_set(
                os.getenv("SUPERLILY_CLAIM_CANARY_CONVERSATIONS_JSON"),
                variable="SUPERLILY_CLAIM_CANARY_CONVERSATIONS_JSON",
            ),
            claim_minimum_confidence=int(os.getenv("SUPERLILY_CLAIM_MINIMUM_CONFIDENCE", "85")),
            claim_required_observations=int(os.getenv("SUPERLILY_CLAIM_REQUIRED_OBSERVATIONS", "2")),
            claim_coalesce_milliseconds=int(os.getenv("SUPERLILY_CLAIM_COALESCE_MILLISECONDS", "200")),
        )
