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
            claim_mode=claim_mode,
            claim_canary_conversations=_string_set(
                os.getenv("SUPERLILY_CLAIM_CANARY_CONVERSATIONS_JSON"),
                variable="SUPERLILY_CLAIM_CANARY_CONVERSATIONS_JSON",
            ),
            claim_minimum_confidence=int(os.getenv("SUPERLILY_CLAIM_MINIMUM_CONFIDENCE", "85")),
            claim_required_observations=int(os.getenv("SUPERLILY_CLAIM_REQUIRED_OBSERVATIONS", "2")),
            claim_coalesce_milliseconds=int(os.getenv("SUPERLILY_CLAIM_COALESCE_MILLISECONDS", "200")),
        )
