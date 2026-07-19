"""第三阶段 Provider 拉取执行契约。

这些模型只表达 lease/fence 协议；身份始终从 Provider bearer
credential 推导，不信任 payload 自报的 Provider。
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import CanonicalJSONError, canonicalize_json_value
from .tool_invocation import OpaqueId
from .tool_registry import ExecutionPermissions, ResourceBudget, SemVer, Sha256, ToolId


TOOL_EXECUTION_SCHEMA_VERSION = "1.0"

AttemptState: TypeAlias = Literal[
    "leased",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "lease_expired",
    "unknown_completion",
]
AttemptEvent: TypeAlias = Literal[
    "lease",
    "start",
    "heartbeat",
    "complete",
    "fail",
    "cancel",
    "lease_expire",
    "reject",
]
ToolFailureCode: TypeAlias = Literal[
    "invalid_input",
    "permission_denied",
    "confirmation_required",
    "rate_limited",
    "budget_exceeded",
    "provider_unavailable",
    "timeout",
    "cancelled",
    "execution_failed",
    "invalid_output",
    "artifact_failed",
    "internal_error",
]

LeaseSecret = Annotated[str, Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
ArtifactUploadSecret = Annotated[
    str,
    Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
_MIME_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)


def _exact_mime(value: str) -> str:
    if not _MIME_RE.fullmatch(value):
        raise ValueError("artifact MIME must be an exact lowercase media type")
    return value


class ExecutionContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        frozen=True,
    )


class ToolUsage(ExecutionContractModel):
    wall_time_ms: int = Field(default=0, ge=0, le=86_400_000)
    cpu_ms: int = Field(default=0, ge=0, le=86_400_000)
    memory_peak_bytes: int = Field(default=0, ge=0, le=1_099_511_627_776)
    input_bytes: int = Field(default=0, ge=0, le=1_073_741_824)
    output_bytes: int = Field(default=0, ge=0, le=1_073_741_824)
    artifact_bytes: int = Field(default=0, ge=0, le=10_737_418_240)


class ToolLeaseRequestIn(ExecutionContractModel):
    schema_version: Literal["1.0"] = TOOL_EXECUTION_SCHEMA_VERSION
    inventory_hash: Sha256


class ToolExecutionProof(ExecutionContractModel):
    schema_version: Literal["1.0"] = TOOL_EXECUTION_SCHEMA_VERSION
    attempt_id: OpaqueId
    fencing_token: int = Field(ge=1, le=9_223_372_036_854_775_807)
    lease_secret: LeaseSecret


class ToolExecutionStartIn(ToolExecutionProof):
    pass


class ToolExecutionHeartbeatIn(ToolExecutionProof):
    usage: ToolUsage = Field(default_factory=ToolUsage)
    provider_observed_at: AwareDatetime | None = None

    @field_validator("provider_observed_at", mode="before")
    @classmethod
    def validate_provider_time(cls, value: Any) -> Any:
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str) or value != value.strip():
            raise ValueError("provider_observed_at must be an exact ISO 8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("provider_observed_at must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("provider_observed_at must include a timezone")
        return parsed


class ToolArtifactReference(ExecutionContractModel):
    artifact_id: OpaqueId
    content_sha256: Sha256
    mime_type: str = Field(min_length=3, max_length=255)
    byte_size: int = Field(ge=1, le=10_737_418_240)
    width_pixels: int | None = Field(default=None, ge=1, le=32_768)
    height_pixels: int | None = Field(default=None, ge=1, le=32_768)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _exact_mime(value)

    @model_validator(mode="after")
    def dimensions_are_paired(self) -> "ToolArtifactReference":
        if (self.width_pixels is None) != (self.height_pixels is None):
            raise ValueError("artifact dimensions must both be present or absent")
        return self


class ToolExecutionCompleteIn(ToolExecutionProof):
    provider_result_id: OpaqueId
    output: Any
    usage: ToolUsage
    artifacts: list[ToolArtifactReference] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_output_domain(self) -> "ToolExecutionCompleteIn":
        try:
            canonicalize_json_value(self.output)
        except CanonicalJSONError as exc:
            raise ValueError("tool output is outside the bounded canonical JSON domain") from exc
        return self


class ToolArtifactReserveIn(ToolExecutionProof):
    mime_type: str = Field(min_length=3, max_length=255)
    declared_bytes: int | None = Field(default=None, ge=1, le=10_737_418_240)
    declared_sha256: Sha256 | None = None

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _exact_mime(value)


class ToolArtifactReservationOut(ExecutionContractModel):
    artifact_id: OpaqueId
    invocation_id: OpaqueId
    attempt_id: OpaqueId
    fencing_token: int = Field(ge=1)
    upload_secret: ArtifactUploadSecret
    mime_type: str
    max_bytes: int = Field(ge=1, le=10_737_418_240)
    max_width_pixels: int = Field(ge=1, le=32_768)
    max_height_pixels: int = Field(ge=1, le=32_768)
    expires_at: AwareDatetime

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _exact_mime(value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expires_at(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or value != value.strip():
            raise ValueError("artifact expiry must be an exact ISO 8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("artifact expiry must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("artifact expiry must include a timezone")
        return parsed


class ToolArtifactUploadOut(ExecutionContractModel):
    artifact_id: OpaqueId
    state: Literal["uploading"]
    content_sha256: Sha256
    mime_type: str
    byte_size: int = Field(ge=1, le=10_737_418_240)
    width_pixels: int | None = Field(default=None, ge=1, le=32_768)
    height_pixels: int | None = Field(default=None, ge=1, le=32_768)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _exact_mime(value)


class ToolArtifactFinalizeIn(ToolExecutionProof):
    artifact_id: OpaqueId
    content_sha256: Sha256
    mime_type: str
    byte_size: int = Field(ge=1, le=10_737_418_240)
    width_pixels: int | None = Field(default=None, ge=1, le=32_768)
    height_pixels: int | None = Field(default=None, ge=1, le=32_768)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _exact_mime(value)

    @model_validator(mode="after")
    def dimensions_are_paired(self) -> "ToolArtifactFinalizeIn":
        if (self.width_pixels is None) != (self.height_pixels is None):
            raise ValueError("artifact dimensions must both be present or absent")
        return self


class ToolExecutionFailIn(ToolExecutionProof):
    provider_result_id: OpaqueId
    error_code: ToolFailureCode
    safe_detail: str = Field(min_length=1, max_length=512)
    usage: ToolUsage = Field(default_factory=ToolUsage)

    @field_validator("safe_detail")
    @classmethod
    def validate_safe_detail(cls, value: str) -> str:
        if (
            value != value.strip()
            or not value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("safe_detail must be exact visible text")
        return value


class ToolLeaseOut(ExecutionContractModel):
    schema_version: Literal["1.0"] = TOOL_EXECUTION_SCHEMA_VERSION
    invocation_id: OpaqueId
    attempt_id: OpaqueId
    attempt_number: int = Field(ge=1)
    fencing_token: int = Field(ge=1)
    lease_secret: LeaseSecret
    provider_id: OpaqueId
    inventory_hash: Sha256
    implementation_hash: Sha256
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    input: Any
    input_hash: Sha256
    deadline_at: AwareDatetime
    lease_expires_at: AwareDatetime
    resource_budget: ResourceBudget
    execution_permissions: ExecutionPermissions

    @field_validator("deadline_at", "lease_expires_at", mode="before")
    @classmethod
    def validate_wire_time(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or value != value.strip():
            raise ValueError("lease timestamps must be exact ISO 8601 values")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("lease timestamps must be ISO 8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("lease timestamps must include a timezone")
        return parsed

    @model_validator(mode="after")
    def validate_canonical_payload(self) -> "ToolLeaseOut":
        try:
            canonical_input = canonicalize_json_value(self.input)
            canonicalize_json_value(self.resource_budget.model_dump(mode="json"))
            canonicalize_json_value(self.execution_permissions.model_dump(mode="json"))
        except CanonicalJSONError as exc:
            raise ValueError("lease payload is outside the bounded canonical JSON domain") from exc
        if canonical_input.sha256 != self.input_hash:
            raise ValueError("lease input does not match its authoritative input_hash")
        if self.lease_expires_at > self.deadline_at:
            raise ValueError("lease expiry must not exceed the invocation deadline")
        return self


def lease_secret_hash(secret: str) -> str:
    """将高熵一次性 secret 转为可比较的存储值。"""

    return canonicalize_json_value({"lease_secret": secret}).sha256


def artifact_upload_secret_hash(secret: str) -> str:
    """将 artifact 一次性上传 secret 转为可比较的存储值。"""

    return canonicalize_json_value({"artifact_upload_secret": secret}).sha256
