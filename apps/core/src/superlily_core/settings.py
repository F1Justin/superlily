import json
import os
from dataclasses import dataclass, field

DEFAULT_DATABASE_URL = "postgresql+asyncpg://superlily:superlily@127.0.0.1:5432/superlily"


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    admin_token: str = ""
    ingest_tokens: dict[str, str] = field(default_factory=dict)
    stale_after_seconds: int = 90
    raw_enabled: bool = False
    raw_max_bytes: int = 32_768

    @classmethod
    def from_env(cls) -> "Settings":
        tokens_raw = os.getenv("SUPERLILY_INGEST_TOKENS_JSON", "{}")
        try:
            tokens = json.loads(tokens_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("SUPERLILY_INGEST_TOKENS_JSON must be valid JSON") from exc
        if not isinstance(tokens, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tokens.items()):
            raise ValueError("SUPERLILY_INGEST_TOKENS_JSON must be an object of string tokens")
        return cls(
            database_url=os.getenv("SUPERLILY_DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_token=os.getenv("SUPERLILY_ADMIN_TOKEN", ""),
            ingest_tokens=tokens,
            stale_after_seconds=int(os.getenv("SUPERLILY_STALE_AFTER_SECONDS", "90")),
            raw_enabled=_as_bool(os.getenv("SUPERLILY_RAW_ENABLED")),
            raw_max_bytes=int(os.getenv("SUPERLILY_RAW_MAX_BYTES", "32768")),
        )
