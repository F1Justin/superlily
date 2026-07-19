"""Phase 3a Tool Registry authority, provider, and schema-profile contracts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Annotated, Any, Literal, TypeAlias

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JSONSchemaValidationError
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import CanonicalJSON, CanonicalJSONError, canonicalize_json, canonicalize_json_value


TOOL_REGISTRY_SCHEMA_VERSION = "1.0"
TOOL_SCHEMA_PROFILE = "json-schema-2020-12-superlily-v1"
PROVIDER_PROTOCOL_V1 = "superlily-provider-pull-v1"

ToolId: TypeAlias = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")]
SemVer: TypeAlias = Annotated[
    str,
    Field(pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"),
]
Sha256: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

SideEffect: TypeAlias = Literal["read", "compute", "write", "admin", "external_message"]
Determinism: TypeAlias = Literal["deterministic", "may_vary", "external_state"]
RetryPolicy: TypeAlias = Literal[
    "retry_safe",
    "no_automatic_retry",
    "provider_idempotency_key_required",
]
Permission: TypeAlias = Literal["public", "trusted", "group_admin", "superuser", "service"]
Confirmation: TypeAlias = Literal["never", "on_write", "always", "two_person"]
Caller: TypeAlias = Literal["command", "agent", "admin_api", "watchdog", "schedule"]
BudgetEnforcement: TypeAlias = Literal["hard", "best_effort", "unsupported"]
ProviderLifecycle: TypeAlias = Literal["registered", "active", "quarantined", "retired", "revoked"]
ProviderHealth: TypeAlias = Literal["starting", "healthy", "degraded", "unavailable", "unknown"]
DescriptorLifecycle: TypeAlias = Literal["draft", "reviewed", "active", "suspended", "retired", "revoked"]
EligibilityReason: TypeAlias = Literal[
    "not_reviewed",
    "inactive_descriptor",
    "provider_missing",
    "provider_stale",
    "provider_unhealthy",
    "provider_quarantined",
    "inventory_missing",
    "inventory_stale",
    "inventory_hash_mismatch",
    "descriptor_missing",
    "descriptor_mismatch",
    "implementation_mismatch",
    "protocol_incompatible",
    "budget_unenforceable",
    "caller_forbidden",
    "principal_unauthorized",
    "capability_unavailable",
    "execution_off",
    "tool_suspended",
    "global_stop",
]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DEF_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_LOCAL_REF_RE = re.compile(r"^#/\$defs/([A-Za-z][A-Za-z0-9_-]{0,63})$")
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")

_ALLOWED_SCHEMA_KEYWORDS = {
    "$schema",
    "$defs",
    "$ref",
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "minProperties",
    "maxProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "enum",
    "const",
}
_COMMON_SCHEMA_KEYWORDS = {"type", "title", "description", "enum", "const", "$schema", "$defs"}
_TYPE_SCHEMA_KEYWORDS = {
    "object": {"properties", "required", "additionalProperties", "minProperties", "maxProperties"},
    "array": {"items", "minItems", "maxItems", "uniqueItems"},
    "string": {"minLength", "maxLength"},
    "integer": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "number": {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"},
    "boolean": set(),
    "null": set(),
}
_BUDGET_NAMES = {"wall_time", "cpu", "memory", "input_bytes", "output_bytes", "artifact_bytes"}
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolRegistryContractError(ValueError):
    """A Tool Registry contract or authority document is invalid."""


class SchemaProfileError(ToolRegistryContractError):
    """A JSON Schema uses syntax outside the restricted Superlily profile."""


class AuthorityModel(BaseModel):
    """Strict authority model that preserves semantic string whitespace."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=False, frozen=True)


def _identifier(value: str, *, label: str) -> str:
    if value != value.strip() or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be an exact opaque identifier")
    return value


def _unique(values: Iterable[str], *, label: str) -> list[str]:
    materialized = list(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} must not contain duplicates")
    return materialized


def _bounded_integer(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SchemaProfileError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _wire_aware_datetime(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value != value.strip():
        raise ValueError("datetime must not contain surrounding whitespace")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("datetime must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return parsed


def _schema_type_matches(value: Any, schema_type: str) -> bool:
    if schema_type == "null":
        return value is None
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "object":
        return isinstance(value, dict)
    return False


def validate_schema_profile(schema: Mapping[str, Any]) -> None:
    """Validate the bounded `json-schema-2020-12-superlily-v1` profile."""

    if not isinstance(schema, dict):
        raise SchemaProfileError("schema root must be an object")
    try:
        canonicalize_json_value(schema)
    except CanonicalJSONError as exc:
        raise SchemaProfileError("schema is outside the bounded RFC 8785 JSON domain") from exc
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise SchemaProfileError("schema must explicitly declare JSON Schema Draft 2020-12")
    definitions = schema.get("$defs", {})
    if not isinstance(definitions, dict):
        raise SchemaProfileError("$defs must be an object")
    if len(definitions) > 64:
        raise SchemaProfileError("schema definition limit exceeded")
    for name in definitions:
        if not isinstance(name, str) or not _DEF_NAME_RE.fullmatch(name):
            raise SchemaProfileError("schema definition names must use the restricted identifier form")

    refs: list[str] = []
    total_schema_nodes = 0

    def visit(node: Any, *, path: str, depth: int, allow_root_keywords: bool = False) -> None:
        nonlocal total_schema_nodes
        total_schema_nodes += 1
        if total_schema_nodes > 2_048:
            raise SchemaProfileError("schema node limit exceeded")
        if depth > 24:
            raise SchemaProfileError("schema depth limit exceeded")
        if not isinstance(node, dict):
            raise SchemaProfileError(f"{path} must be a schema object; boolean schemas are forbidden")
        unknown = set(node) - _ALLOWED_SCHEMA_KEYWORDS
        if unknown:
            raise SchemaProfileError(f"{path} contains unknown or forbidden keywords: {sorted(unknown)}")
        if not allow_root_keywords and ("$schema" in node or "$defs" in node):
            raise SchemaProfileError(f"{path} may not redefine $schema or $defs")
        if "$schema" in node and node["$schema"] != "https://json-schema.org/draft/2020-12/schema":
            raise SchemaProfileError("schema must declare JSON Schema Draft 2020-12")
        if "$ref" in node:
            if set(node) != {"$ref"}:
                raise SchemaProfileError(f"{path} $ref must not have sibling keywords")
            reference = node["$ref"]
            if not isinstance(reference, str):
                raise SchemaProfileError(f"{path} $ref must be a string")
            match = _LOCAL_REF_RE.fullmatch(reference)
            if match is None:
                raise SchemaProfileError(f"{path} permits only local #/$defs/name references")
            refs.append(match.group(1))
            return

        schema_type = node.get("type")
        if not isinstance(schema_type, str) or schema_type not in _TYPE_SCHEMA_KEYWORDS:
            raise SchemaProfileError(f"{path} requires one explicit supported type")
        invalid_for_type = set(node) - _COMMON_SCHEMA_KEYWORDS - _TYPE_SCHEMA_KEYWORDS[schema_type]
        if invalid_for_type:
            raise SchemaProfileError(f"{path} uses keywords incompatible with type {schema_type}")

        if "title" in node and (not isinstance(node["title"], str) or len(node["title"]) > 256):
            raise SchemaProfileError(f"{path} title must be a string of at most 256 characters")
        if "description" in node and (
            not isinstance(node["description"], str) or len(node["description"]) > 4_096
        ):
            raise SchemaProfileError(f"{path} description must be a string of at most 4096 characters")

        for keyword in ("enum", "const"):
            if keyword not in node:
                continue
            values = node[keyword] if keyword == "enum" else [node[keyword]]
            if keyword == "enum" and (not isinstance(values, list) or not 1 <= len(values) <= 128):
                raise SchemaProfileError(f"{path} enum must contain between 1 and 128 values")
            canonical_values: set[bytes] = set()
            for item in values:
                if not _schema_type_matches(item, schema_type):
                    raise SchemaProfileError(f"{path} {keyword} value does not match type {schema_type}")
                encoded = canonicalize_json_value(item).canonical_bytes
                if encoded in canonical_values:
                    raise SchemaProfileError(f"{path} enum values must be unique")
                canonical_values.add(encoded)

        if schema_type == "object":
            properties = node.get("properties")
            if not isinstance(properties, dict):
                raise SchemaProfileError(f"{path} object schema requires properties")
            max_properties = _bounded_integer(
                node.get("maxProperties"), label=f"{path}.maxProperties", minimum=0, maximum=256
            )
            min_properties = _bounded_integer(
                node.get("minProperties", 0), label=f"{path}.minProperties", minimum=0, maximum=256
            )
            if min_properties > max_properties or len(properties) > max_properties:
                raise SchemaProfileError(f"{path} object property bounds are inconsistent")
            if node.get("additionalProperties") is not False:
                raise SchemaProfileError(f"{path} object schema must set additionalProperties=false")
            required = node.get("required", [])
            if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
                raise SchemaProfileError(f"{path} required must be a string array")
            if len(required) != len(set(required)) or not set(required).issubset(properties):
                raise SchemaProfileError(f"{path} required entries must be unique declared properties")
            for name, child in properties.items():
                if not isinstance(name, str) or not name or len(name) > 128:
                    raise SchemaProfileError(f"{path} property names must be 1 to 128 characters")
                visit(child, path=f"{path}.properties[{name!r}]", depth=depth + 1)
        elif schema_type == "array":
            max_items = _bounded_integer(
                node.get("maxItems"), label=f"{path}.maxItems", minimum=0, maximum=1_024
            )
            min_items = _bounded_integer(
                node.get("minItems", 0), label=f"{path}.minItems", minimum=0, maximum=1_024
            )
            if min_items > max_items:
                raise SchemaProfileError(f"{path} array item bounds are inconsistent")
            if "items" not in node:
                raise SchemaProfileError(f"{path} array schema requires items")
            if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
                raise SchemaProfileError(f"{path} uniqueItems must be boolean")
            visit(node["items"], path=f"{path}.items", depth=depth + 1)
        elif schema_type == "string":
            max_length = _bounded_integer(
                node.get("maxLength"), label=f"{path}.maxLength", minimum=0, maximum=262_144
            )
            min_length = _bounded_integer(
                node.get("minLength", 0), label=f"{path}.minLength", minimum=0, maximum=262_144
            )
            if min_length > max_length:
                raise SchemaProfileError(f"{path} string length bounds are inconsistent")
        elif schema_type in {"integer", "number"}:
            lower = node.get("minimum", node.get("exclusiveMinimum"))
            upper = node.get("maximum", node.get("exclusiveMaximum"))
            if lower is None or upper is None:
                raise SchemaProfileError(f"{path} numeric schema requires finite lower and upper bounds")
            if (
                isinstance(lower, bool)
                or isinstance(upper, bool)
                or not isinstance(lower, (int, float))
                or not isinstance(upper, (int, float))
            ):
                raise SchemaProfileError(f"{path} numeric bounds must be numbers")
            canonicalize_json_value(lower)
            canonicalize_json_value(upper)
            if lower >= upper:
                raise SchemaProfileError(f"{path} numeric lower bound must be less than upper bound")
            if "minimum" in node and "exclusiveMinimum" in node:
                raise SchemaProfileError(f"{path} cannot combine minimum and exclusiveMinimum")
            if "maximum" in node and "exclusiveMaximum" in node:
                raise SchemaProfileError(f"{path} cannot combine maximum and exclusiveMaximum")
            if "multipleOf" in node:
                multiple = node["multipleOf"]
                if isinstance(multiple, bool) or not isinstance(multiple, (int, float)) or multiple <= 0:
                    raise SchemaProfileError(f"{path} multipleOf must be a positive number")
                canonicalize_json_value(multiple)

    visit(schema, path="$", depth=0, allow_root_keywords=True)
    for name, definition in definitions.items():
        visit(definition, path=f"$.$defs[{name!r}]", depth=1)
    if len(refs) > 256:
        raise SchemaProfileError("schema local-reference count exceeded")
    missing = sorted(set(refs) - set(definitions))
    if missing:
        raise SchemaProfileError(f"schema references missing definitions: {missing}")

    def referenced_definitions(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str):
                match = _LOCAL_REF_RE.fullmatch(reference)
                if match is not None:
                    found.add(match.group(1))
            for key, item in node.items():
                if key != "$defs":
                    found.update(referenced_definitions(item))
        elif isinstance(node, list):
            for item in node:
                found.update(referenced_definitions(item))
        return found

    graph = {name: referenced_definitions(definition) for name, definition in definitions.items()}

    def check_reference_chain(name: str, stack: tuple[str, ...]) -> None:
        if name in stack:
            raise SchemaProfileError(f"schema definition cycle detected: {' -> '.join((*stack, name))}")
        if len(stack) >= 8:
            raise SchemaProfileError("schema reference expansion depth exceeded")
        for target in graph.get(name, set()):
            check_reference_chain(target, (*stack, name))

    for definition_name in graph:
        check_reference_chain(definition_name, ())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaProfileError("schema fails Draft 2020-12 meta-schema validation") from exc


def validate_schema_instance(instance: Any, schema: Mapping[str, Any]) -> None:
    """Validate data only after the restricted profile has been accepted."""

    validate_schema_profile(schema)
    try:
        canonicalize_json_value(instance)
        Draft202012Validator(schema).validate(instance)
    except (CanonicalJSONError, JSONSchemaValidationError) as exc:
        message = exc.message if isinstance(exc, JSONSchemaValidationError) else str(exc)
        raise ToolRegistryContractError(f"instance does not satisfy tool schema: {message}") from exc


class ProviderSelector(AuthorityModel):
    provider_ids: list[str] = Field(min_length=1, max_length=32)
    protocol: Literal["superlily-provider-pull-v1"]

    @field_validator("provider_ids")
    @classmethod
    def validate_provider_ids(cls, value: list[str]) -> list[str]:
        for item in value:
            _identifier(item, label="provider_id")
        return _unique(value, label="provider_ids")


class RateLimit(AuthorityModel):
    requests: int = Field(ge=1, le=1_000_000)
    window_seconds: int = Field(ge=1, le=86_400)
    scope: Literal["global", "tool", "provider", "conversation", "sender"]


class ResourceBudget(AuthorityModel):
    cpu_ms: int | None = Field(default=None, ge=1, le=86_400_000)
    memory_bytes: int | None = Field(default=None, ge=1_048_576, le=1_099_511_627_776)
    input_bytes: int | None = Field(default=None, ge=1, le=1_073_741_824)
    output_bytes: int | None = Field(default=None, ge=1, le=1_073_741_824)
    artifact_bytes: int | None = Field(default=None, ge=1, le=10_737_418_240)

    @model_validator(mode="after")
    def require_budget(self) -> "ResourceBudget":
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("resource_budget must define at least one bound")
        return self


class ExecutionPermissions(AuthorityModel):
    network: Literal["deny"]
    filesystem: Literal["deny", "sandbox_only"]
    subprocess: Literal["deny"]
    secrets: list[str] = Field(max_length=32)
    remote_fetch: Literal["deny"]
    artifacts: list[str] = Field(max_length=32)

    @field_validator("secrets")
    @classmethod
    def validate_secrets(cls, value: list[str]) -> list[str]:
        for item in value:
            _identifier(item, label="secret name")
        return _unique(value, label="secrets")

    @field_validator("artifacts")
    @classmethod
    def validate_artifacts(cls, value: list[str]) -> list[str]:
        if any(not _MIME_RE.fullmatch(item) for item in value):
            raise ValueError("artifact permissions must be lowercase exact MIME types")
        return _unique(value, label="artifacts")


class ArtifactPolicy(AuthorityModel):
    max_count: int = Field(ge=1, le=32)
    max_single_bytes: int = Field(ge=1, le=1_073_741_824)
    max_width_pixels: int = Field(ge=1, le=32_768)
    max_height_pixels: int = Field(ge=1, le=32_768)
    reservation_ttl_seconds: int = Field(ge=10, le=900)


class ToolDescriptor(AuthorityModel):
    tool_id: ToolId
    version: SemVer
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4_096)
    provider_selector: ProviderSelector
    source_plugin: str = Field(min_length=1, max_length=512)
    schema_profile: Literal["json-schema-2020-12-superlily-v1"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    side_effect: SideEffect
    determinism: Determinism
    retry_policy: RetryPolicy
    permission: Permission
    confirmation: Confirmation
    allowed_callers: list[Caller] = Field(min_length=1, max_length=8)
    natural_language: bool
    timeout_ms: int = Field(ge=1, le=3_600_000)
    concurrency_limit: int = Field(ge=1, le=10_000)
    rate_limit: RateLimit
    resource_budget: ResourceBudget
    required_budget_enforcement: list[str] = Field(min_length=1, max_length=16)
    execution_permissions: ExecutionPermissions
    artifact_policy: ArtifactPolicy | None = None
    required_capabilities: list[str] = Field(max_length=64)
    data_classification: Literal["public", "conversation", "sensitive", "administrative"]
    result_retention_seconds: int = Field(ge=0, le=31_536_000)

    @field_validator("title", "description")
    @classmethod
    def require_visible_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("descriptor text must contain a non-whitespace character")
        return value

    @field_validator("source_plugin")
    @classmethod
    def validate_source_plugin(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("source_plugin must not contain surrounding whitespace")
        return value

    @field_validator("allowed_callers")
    @classmethod
    def validate_callers(cls, value: list[Caller]) -> list[Caller]:
        return _unique(value, label="allowed_callers")  # type: ignore[return-value]

    @field_validator("required_budget_enforcement")
    @classmethod
    def validate_required_budgets(cls, value: list[str]) -> list[str]:
        if any(item not in _BUDGET_NAMES for item in value):
            raise ValueError("required_budget_enforcement contains an unknown budget")
        return _unique(value, label="required_budget_enforcement")

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if any(not _CAPABILITY_RE.fullmatch(item) for item in value):
            raise ValueError("required capabilities must use lowercase identifiers")
        return _unique(value, label="required_capabilities")

    @model_validator(mode="after")
    def validate_phase_three_authority(self) -> "ToolDescriptor":
        validate_schema_profile(self.input_schema)
        validate_schema_profile(self.output_schema)
        if self.natural_language or "agent" in self.allowed_callers:
            raise ValueError("natural-language and agent callers remain disabled in Phase 3")
        if self.side_effect in {"write", "admin", "external_message"} and self.confirmation == "never":
            raise ValueError("state-changing tools require confirmation")
        if self.side_effect in {"write", "admin", "external_message"} and self.retry_policy == "retry_safe":
            raise ValueError("state-changing tools cannot declare unconditional retry safety")
        artifact_mimes = self.execution_permissions.artifacts
        artifact_budget = self.resource_budget.artifact_bytes
        if not artifact_mimes:
            if self.artifact_policy is not None or artifact_budget is not None:
                raise ValueError("artifact-free tools cannot declare artifact policy or budget")
        else:
            if self.artifact_policy is None or artifact_budget is None:
                raise ValueError("artifact permissions require policy and total byte budget")
            if self.artifact_policy.max_single_bytes > artifact_budget:
                raise ValueError("single artifact bound cannot exceed total artifact budget")
            if "artifact_bytes" not in self.required_budget_enforcement:
                raise ValueError("artifact byte budget must require hard enforcement")
        return self


@dataclass(frozen=True, slots=True)
class LoadedToolDescriptor:
    descriptor: ToolDescriptor
    authority: CanonicalJSON


def load_tool_descriptor(source: bytes) -> LoadedToolDescriptor:
    """Load, validate, and content-address one reviewed descriptor document."""

    try:
        authority = canonicalize_json(source)
        if not isinstance(authority.value, dict):
            raise ToolRegistryContractError("tool descriptor root must be an object")
        descriptor = ToolDescriptor.model_validate(authority.value)
    except ToolRegistryContractError:
        raise
    except (CanonicalJSONError, ValueError) as exc:
        raise ToolRegistryContractError("tool descriptor authority document is invalid") from exc
    return LoadedToolDescriptor(descriptor=descriptor, authority=authority)


class ProviderRegistration(AuthorityModel):
    provider_id: str
    owner: str = Field(min_length=1, max_length=256)
    lifecycle: ProviderLifecycle
    allowed_protocols: list[Literal["superlily-provider-pull-v1"]] = Field(min_length=1, max_length=8)
    tool_selectors: list[ToolId] = Field(min_length=1, max_length=256)

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        return _identifier(value, label="provider_id")

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        if value != value.strip() or not value:
            raise ValueError("provider owner must not contain surrounding whitespace")
        return value

    @field_validator("allowed_protocols", "tool_selectors")
    @classmethod
    def validate_registration_lists(cls, value: list[str]) -> list[str]:
        return _unique(value, label="provider registration list")


class ProviderInventoryTool(AuthorityModel):
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    protocol_version: Literal["superlily-provider-pull-v1"]
    implementation_hash: Sha256
    budget_enforcement: dict[str, BudgetEnforcement] = Field(max_length=16)

    @field_validator("budget_enforcement")
    @classmethod
    def validate_budget_enforcement(cls, value: dict[str, BudgetEnforcement]) -> dict[str, BudgetEnforcement]:
        if any(key not in _BUDGET_NAMES for key in value):
            raise ValueError("provider inventory contains an unknown budget")
        return dict(sorted(value.items()))


class ProviderInventorySnapshotIn(AuthorityModel):
    schema_version: Literal["1.0"] = TOOL_REGISTRY_SCHEMA_VERSION
    provider_id: str
    snapshot_hash: Sha256
    observed_at: AwareDatetime
    protocol_version: Literal["superlily-provider-pull-v1"]
    tools: list[ProviderInventoryTool] = Field(max_length=1_024)

    @field_validator("observed_at", mode="before")
    @classmethod
    def validate_observed_at(cls, value: Any) -> Any:
        return _wire_aware_datetime(value)

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        return _identifier(value, label="provider_id")

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: list[ProviderInventoryTool]) -> list[ProviderInventoryTool]:
        identities = [item.tool_id for item in value]
        if len(identities) != len(set(identities)):
            raise ValueError("provider inventory tool IDs must be unique")
        return value

    @model_validator(mode="after")
    def verify_snapshot_hash(self) -> "ProviderInventorySnapshotIn":
        expected = provider_inventory_snapshot_hash(
            provider_id=self.provider_id,
            protocol_version=self.protocol_version,
            tools=self.tools,
        )
        if self.snapshot_hash != expected:
            raise ValueError("provider inventory snapshot hash mismatch")
        return self


class ProviderHeartbeatIn(AuthorityModel):
    schema_version: Literal["1.0"] = TOOL_REGISTRY_SCHEMA_VERSION
    provider_id: str
    inventory_hash: Sha256
    observed_at: AwareDatetime
    health: ProviderHealth
    current_concurrency: int = Field(ge=0, le=10_000)
    max_concurrency: int = Field(ge=1, le=10_000)
    oldest_work_age_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @field_validator("observed_at", mode="before")
    @classmethod
    def validate_observed_at(cls, value: Any) -> Any:
        return _wire_aware_datetime(value)

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        return _identifier(value, label="provider_id")

    @model_validator(mode="after")
    def validate_capacity(self) -> "ProviderHeartbeatIn":
        if self.current_concurrency > self.max_concurrency:
            raise ValueError("current_concurrency cannot exceed max_concurrency")
        canonicalize_json_value(self.metadata)
        return self


def provider_inventory_snapshot_hash(
    *,
    provider_id: str,
    protocol_version: str,
    tools: Iterable[ProviderInventoryTool],
) -> str:
    """Hash stable inventory content independently from observation time."""

    if protocol_version != PROVIDER_PROTOCOL_V1:
        raise ToolRegistryContractError("provider inventory uses an unsupported protocol")
    materialized = sorted(
        (tool.model_dump(mode="json") for tool in tools),
        key=lambda item: (item["tool_id"], item["descriptor_version"], item["descriptor_hash"]),
    )
    payload = {
        "provider_id": _identifier(provider_id, label="provider_id"),
        "protocol_version": protocol_version,
        "tools": materialized,
    }
    return canonicalize_json_value(payload).sha256
