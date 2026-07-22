import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import stat

from .command_registry import DEFAULT_COMMAND_REGISTRY_PATH

DEFAULT_DATABASE_URL = "postgresql+asyncpg://superlily:superlily@127.0.0.1:5432/superlily"


@dataclass(frozen=True, slots=True)
class ControlOperator:
    operator_id: str
    role: str
    password_hash: str = field(repr=False)
    enabled: bool = True


_CONTROL_OPERATOR_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
_SCRYPT_HASH_RE = re.compile(
    r"^scrypt\$16384\$8\$1\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{43}$"
)
_CONTROL_ROLES = {"auditor", "operator", "reviewer", "security_admin", "break_glass"}


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _token_map(value: str | None, *, variable: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be valid JSON") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and key and isinstance(token, str) and token
        for key, token in parsed.items()
    ):
        raise ValueError(
            f"{variable} must be an object of non-empty string IDs to non-empty string tokens"
        )
    return parsed


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


def _secret_file(value: str | None, *, variable: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError(f"{variable} must be an absolute secret file path")
    try:
        entry = path.lstat()
    except OSError as exc:
        raise ValueError(f"{variable} is unavailable") from exc
    if not stat.S_ISREG(entry.st_mode) or path.is_symlink() or entry.st_mode & 0o022:
        raise ValueError(f"{variable} failed authority checks")
    secret = path.read_text(encoding="utf-8").strip()
    if len(secret) < 32:
        raise ValueError(f"{variable} must contain at least 32 characters")
    return secret


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


def _control_operators(value: str | None, *, variable: str) -> dict[str, ControlOperator]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"schema_version", "operators"}:
        raise ValueError(f"{variable} must contain schema_version and operators")
    if parsed["schema_version"] != "1.0" or not isinstance(parsed["operators"], list):
        raise ValueError(f"{variable} must use schema_version 1.0 and an operators array")
    operators: dict[str, ControlOperator] = {}
    expected = {"operator_id", "role", "password_hash", "enabled"}
    for raw in parsed["operators"]:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError(f"{variable} operator entries must contain the exact fields")
        if (
            not isinstance(raw["operator_id"], str)
            or not isinstance(raw["role"], str)
            or not isinstance(raw["password_hash"], str)
            or not isinstance(raw["enabled"], bool)
        ):
            raise ValueError(f"{variable} operator fields have invalid types")
        operator_id = raw["operator_id"]
        role = raw["role"]
        if not _CONTROL_OPERATOR_RE.fullmatch(operator_id):
            raise ValueError(f"{variable} contains an invalid operator_id")
        if role not in _CONTROL_ROLES:
            raise ValueError(f"{variable} contains an invalid role")
        if not _SCRYPT_HASH_RE.fullmatch(raw["password_hash"]):
            raise ValueError(f"{variable} password_hash must use the bounded scrypt format")
        if operator_id in operators:
            raise ValueError(f"{variable} must not contain duplicate operator IDs")
        operators[operator_id] = ControlOperator(
            operator_id=operator_id,
            role=role,
            password_hash=raw["password_hash"],
            enabled=raw["enabled"],
        )
    if len(operators) > 100:
        raise ValueError(f"{variable} must contain at most 100 operators")
    return operators


def _reject_obsolete_tool_rollout_scope(value: str | None, *, variable: str) -> None:
    if value is None or not value.strip():
        return
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{variable} is obsolete; import a reviewed database rollout plan"
        ) from exc
    if parsed != []:
        raise ValueError(
            f"{variable} is obsolete; import a reviewed database rollout plan"
        )


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    admin_token: str = ""
    ingest_tokens: dict[str, str] = field(default_factory=dict)
    provider_tokens: dict[str, str] = field(default_factory=dict)
    stale_after_seconds: int = 90
    correlation_window_seconds: int = 2
    raw_enabled: bool = False
    raw_max_bytes: int = 32_768
    command_registry_path: str = DEFAULT_COMMAND_REGISTRY_PATH
    command_registry_snapshot_stale_seconds: int = 600
    provider_inventory_stale_seconds: int = 600
    provider_heartbeat_stale_seconds: int = 90
    tool_execution_mode: str = "off"
    tool_global_stop: bool = False
    tool_lease_seconds: int = 15
    tool_confirmation_seconds: int = 120
    tool_reaper_interval_seconds: int = 1
    artifact_root: str = ""
    artifact_secret_pepper: str = field(default="", repr=False)
    artifact_orphan_grace_seconds: int = 300
    render_mode: str = "off"
    render_canary_conversations: frozenset[str] = field(default_factory=frozenset)
    render_backend_url: str = ""
    render_backend_token: str = field(default="", repr=False)
    render_implementation_hash: str = ""
    render_timeout_seconds: int = 30
    render_artifact_ttl_seconds: int = 3_600
    render_delivery_intent_seconds: int = 60
    control_operators: dict[str, ControlOperator] = field(default_factory=dict, repr=False)
    control_allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    control_allowed_origins: frozenset[str] = field(default_factory=frozenset)
    control_audit_pepper: str = field(default="", repr=False)
    control_session_seconds: int = 900
    control_reauth_seconds: int = 300
    control_login_attempts: int = 5
    control_login_window_seconds: int = 300
    control_preview_seconds: int = 60
    control_mutation_attempts: int = 10
    control_mutation_window_seconds: int = 60
    group_default_mode: str = "command_only"
    group_modes: dict[str, str] = field(default_factory=dict)
    claim_mode: str = "off"
    claim_canary_conversations: frozenset[str] = field(default_factory=frozenset)
    claim_minimum_confidence: int = 85
    claim_required_observations: int = 2
    claim_coalesce_milliseconds: int = 200

    def __post_init__(self) -> None:
        active_ingest_tokens = [token for token in self.ingest_tokens.values() if token]
        active_provider_tokens = [token for token in self.provider_tokens.values() if token]
        if len(active_ingest_tokens) != len(set(active_ingest_tokens)):
            raise ValueError("ingest tokens must be unique per instance")
        if len(active_provider_tokens) != len(set(active_provider_tokens)):
            raise ValueError("provider tokens must be unique per provider")
        if self.admin_token and self.admin_token in active_ingest_tokens:
            raise ValueError("admin and ingest tokens must be unrelated")
        all_bot_admin_tokens = set(active_ingest_tokens)
        if self.admin_token:
            all_bot_admin_tokens.add(self.admin_token)
        if set(active_provider_tokens) & all_bot_admin_tokens:
            raise ValueError("provider, admin, and ingest tokens must be unrelated")
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
        if not 1 <= self.provider_inventory_stale_seconds <= 86_400:
            raise ValueError("provider_inventory_stale_seconds must be between 1 and 86400")
        if not 1 <= self.provider_heartbeat_stale_seconds <= 86_400:
            raise ValueError("provider_heartbeat_stale_seconds must be between 1 and 86400")
        if self.tool_execution_mode not in {"off", "ledger_only", "canary"}:
            raise ValueError(
                "tool_execution_mode must be off, ledger_only, or canary; enforce is not open"
            )
        if not 1 <= self.tool_lease_seconds <= 300:
            raise ValueError("tool_lease_seconds must be between 1 and 300")
        if not 30 <= self.tool_confirmation_seconds <= 900:
            raise ValueError("tool_confirmation_seconds must be between 30 and 900")
        if not 1 <= self.tool_reaper_interval_seconds <= 60:
            raise ValueError("tool_reaper_interval_seconds must be between 1 and 60")
        if bool(self.artifact_root) != bool(self.artifact_secret_pepper):
            raise ValueError("artifact root and secret pepper must be configured together")
        if self.artifact_root:
            root = Path(self.artifact_root)
            if (
                not root.is_absolute()
                or ".." in root.parts
                or "\x00" in self.artifact_root
                or root == Path("/")
                or root == Path.home()
            ):
                raise ValueError("artifact_root must be a narrow absolute directory")
            if len(self.artifact_secret_pepper) < 32:
                raise ValueError("artifact_secret_pepper must contain at least 32 characters")
        if not 60 <= self.artifact_orphan_grace_seconds <= 86_400:
            raise ValueError("artifact_orphan_grace_seconds must be between 60 and 86400")
        if self.render_mode not in {"off", "canary"}:
            raise ValueError("render_mode must be off or canary")
        if self.render_mode == "canary" and (
            not self.render_canary_conversations
            or not self.render_backend_url
            or not self.render_backend_token
            or not re.fullmatch(r"[0-9a-f]{64}", self.render_implementation_hash)
        ):
            raise ValueError(
                "render canary requires conversations, backend URL, token, and implementation hash"
            )
        if self.render_backend_url and not re.fullmatch(
            r"http://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", self.render_backend_url
        ):
            raise ValueError("render_backend_url must be an exact internal HTTP origin")
        if self.render_backend_token and len(self.render_backend_token) < 32:
            raise ValueError("render_backend_token must contain at least 32 characters")
        if not 5 <= self.render_timeout_seconds <= 120:
            raise ValueError("render_timeout_seconds must be between 5 and 120")
        if not 300 <= self.render_artifact_ttl_seconds <= 86_400:
            raise ValueError("render_artifact_ttl_seconds must be between 300 and 86400")
        if not 10 <= self.render_delivery_intent_seconds <= 300:
            raise ValueError("render_delivery_intent_seconds must be between 10 and 300")
        if any(
            not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?", host)
            or "/" in host
            for host in self.control_allowed_hosts
        ):
            raise ValueError("control_allowed_hosts contains an invalid exact Host")
        if any(
            not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", origin)
            for origin in self.control_allowed_origins
        ):
            raise ValueError("control_allowed_origins must contain exact HTTPS origins")
        if self.control_operators and (
            not self.control_allowed_hosts
            or not self.control_allowed_origins
            or len(self.control_audit_pepper) < 32
        ):
            raise ValueError(
                "control operators require allowed hosts, allowed origins, and a 32-byte audit pepper"
            )
        if not 60 <= self.control_session_seconds <= 3_600:
            raise ValueError("control_session_seconds must be between 60 and 3600")
        if not 30 <= self.control_reauth_seconds <= self.control_session_seconds:
            raise ValueError("control_reauth_seconds must be between 30 and session lifetime")
        if not 1 <= self.control_login_attempts <= 20:
            raise ValueError("control_login_attempts must be between 1 and 20")
        if not 60 <= self.control_login_window_seconds <= 3_600:
            raise ValueError("control_login_window_seconds must be between 60 and 3600")
        if not 15 <= self.control_preview_seconds <= 300:
            raise ValueError("control_preview_seconds must be between 15 and 300")
        if not 1 <= self.control_mutation_attempts <= 100:
            raise ValueError("control_mutation_attempts must be between 1 and 100")
        if not 60 <= self.control_mutation_window_seconds <= 3_600:
            raise ValueError("control_mutation_window_seconds must be between 60 and 3600")
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

    @property
    def artifact_enabled(self) -> bool:
        return bool(self.artifact_root and self.artifact_secret_pepper)

    @property
    def render_enabled(self) -> bool:
        return self.render_mode == "canary" and self.artifact_enabled

    @classmethod
    def from_env(cls) -> "Settings":
        tokens = _token_map(
            os.getenv("SUPERLILY_INGEST_TOKENS_JSON", "{}"),
            variable="SUPERLILY_INGEST_TOKENS_JSON",
        )
        provider_tokens = _token_map(
            os.getenv("SUPERLILY_PROVIDER_TOKENS_JSON", "{}"),
            variable="SUPERLILY_PROVIDER_TOKENS_JSON",
        )
        claim_mode = os.getenv("SUPERLILY_CLAIM_MODE", "off").strip().lower()
        if claim_mode not in {"off", "shadow", "canary", "enforce"}:
            raise ValueError("SUPERLILY_CLAIM_MODE must be off, shadow, canary, or enforce")
        _reject_obsolete_tool_rollout_scope(
            os.getenv("SUPERLILY_TOOL_CANARY_SCOPES_JSON"),
            variable="SUPERLILY_TOOL_CANARY_SCOPES_JSON",
        )
        _reject_obsolete_tool_rollout_scope(
            os.getenv("SUPERLILY_TOOL_ENFORCE_SCOPES_JSON"),
            variable="SUPERLILY_TOOL_ENFORCE_SCOPES_JSON",
        )
        return cls(
            database_url=os.getenv("SUPERLILY_DATABASE_URL", DEFAULT_DATABASE_URL),
            admin_token=os.getenv("SUPERLILY_ADMIN_TOKEN", ""),
            ingest_tokens=tokens,
            provider_tokens=provider_tokens,
            stale_after_seconds=int(os.getenv("SUPERLILY_STALE_AFTER_SECONDS", "90")),
            correlation_window_seconds=int(os.getenv("SUPERLILY_CORRELATION_WINDOW_SECONDS", "2")),
            raw_enabled=_as_bool(os.getenv("SUPERLILY_RAW_ENABLED")),
            raw_max_bytes=int(os.getenv("SUPERLILY_RAW_MAX_BYTES", "32768")),
            command_registry_path=os.getenv("SUPERLILY_COMMAND_REGISTRY_PATH", DEFAULT_COMMAND_REGISTRY_PATH),
            command_registry_snapshot_stale_seconds=int(
                os.getenv("SUPERLILY_COMMAND_REGISTRY_SNAPSHOT_STALE_SECONDS", "600")
            ),
            provider_inventory_stale_seconds=int(
                os.getenv("SUPERLILY_PROVIDER_INVENTORY_STALE_SECONDS", "600")
            ),
            provider_heartbeat_stale_seconds=int(
                os.getenv("SUPERLILY_PROVIDER_HEARTBEAT_STALE_SECONDS", "90")
            ),
            tool_execution_mode=os.getenv(
                "SUPERLILY_TOOL_EXECUTION_MODE",
                "off",
            ).strip().lower(),
            tool_global_stop=_as_bool(os.getenv("SUPERLILY_TOOL_GLOBAL_STOP")),
            tool_lease_seconds=int(os.getenv("SUPERLILY_TOOL_LEASE_SECONDS", "15")),
            tool_confirmation_seconds=int(
                os.getenv("SUPERLILY_TOOL_CONFIRMATION_SECONDS", "120")
            ),
            tool_reaper_interval_seconds=int(
                os.getenv("SUPERLILY_TOOL_REAPER_INTERVAL_SECONDS", "1")
            ),
            artifact_root=os.getenv("SUPERLILY_ARTIFACT_ROOT", ""),
            artifact_secret_pepper=os.getenv("SUPERLILY_ARTIFACT_SECRET_PEPPER", ""),
            artifact_orphan_grace_seconds=int(
                os.getenv("SUPERLILY_ARTIFACT_ORPHAN_GRACE_SECONDS", "300")
            ),
            render_mode=os.getenv("SUPERLILY_RENDER_MODE", "off").strip().lower(),
            render_canary_conversations=_string_set(
                os.getenv("SUPERLILY_RENDER_CANARY_CONVERSATIONS_JSON"),
                variable="SUPERLILY_RENDER_CANARY_CONVERSATIONS_JSON",
            ),
            render_backend_url=os.getenv("SUPERLILY_RENDER_BACKEND_URL", ""),
            render_backend_token=_secret_file(
                os.getenv("SUPERLILY_RENDER_BACKEND_TOKEN_FILE"),
                variable="SUPERLILY_RENDER_BACKEND_TOKEN_FILE",
            ),
            render_implementation_hash=os.getenv(
                "SUPERLILY_RENDER_IMPLEMENTATION_HASH", ""
            ).strip(),
            render_timeout_seconds=int(os.getenv("SUPERLILY_RENDER_TIMEOUT_SECONDS", "30")),
            render_artifact_ttl_seconds=int(
                os.getenv("SUPERLILY_RENDER_ARTIFACT_TTL_SECONDS", "3600")
            ),
            render_delivery_intent_seconds=int(
                os.getenv("SUPERLILY_RENDER_DELIVERY_INTENT_SECONDS", "60")
            ),
            control_operators=_control_operators(
                os.getenv("SUPERLILY_CONTROL_OPERATORS_JSON"),
                variable="SUPERLILY_CONTROL_OPERATORS_JSON",
            ),
            control_allowed_hosts=_string_set(
                os.getenv("SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON"),
                variable="SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON",
            ),
            control_allowed_origins=_string_set(
                os.getenv("SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON"),
                variable="SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON",
            ),
            control_audit_pepper=os.getenv("SUPERLILY_CONTROL_AUDIT_PEPPER", ""),
            control_session_seconds=int(
                os.getenv("SUPERLILY_CONTROL_SESSION_SECONDS", "900")
            ),
            control_reauth_seconds=int(
                os.getenv("SUPERLILY_CONTROL_REAUTH_SECONDS", "300")
            ),
            control_login_attempts=int(
                os.getenv("SUPERLILY_CONTROL_LOGIN_ATTEMPTS", "5")
            ),
            control_login_window_seconds=int(
                os.getenv("SUPERLILY_CONTROL_LOGIN_WINDOW_SECONDS", "300")
            ),
            control_preview_seconds=int(
                os.getenv("SUPERLILY_CONTROL_PREVIEW_SECONDS", "60")
            ),
            control_mutation_attempts=int(
                os.getenv("SUPERLILY_CONTROL_MUTATION_ATTEMPTS", "10")
            ),
            control_mutation_window_seconds=int(
                os.getenv("SUPERLILY_CONTROL_MUTATION_WINDOW_SECONDS", "60")
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
