"""第五阶段 AgentRun、模型 Provider 与 planner-only shadow 合同。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .canonical_json import CanonicalJSONError, canonicalize_json_value


AGENT_RUN_SCHEMA_VERSION = "1.0"
AGENT_CONTEXT_RECIPE_VERSION = "phase5-context-v1"

Sha256: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SemVer: TypeAlias = Annotated[
    str,
    Field(
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"
        )
    ),
]
ToolId: TypeAlias = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"),
]

DataClassification = Literal["public", "conversation", "sensitive", "administrative"]
AgentAttemptOutcome = Literal[
    "succeeded",
    "provider_error",
    "invalid_output",
    "timed_out",
    "cancelled",
]
AgentProposalValidation = Literal[
    "valid",
    "invalid_arguments",
    "forbidden_tool",
    "duplicate_loop",
]

_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class AgentContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        frozen=True,
    )


def _parse_wire_aware_datetime(value: object) -> object:
    if not isinstance(value, str):
        return value
    if value != value.strip():
        raise ValueError("datetime must not contain surrounding whitespace")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("datetime must use ISO 8601 format") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return parsed


class ModelPricing(AgentContractModel):
    currency: Literal["USD"]
    input_cache_hit_microunits_per_million_tokens: int = Field(ge=0, le=10**12)
    input_cache_miss_microunits_per_million_tokens: int = Field(ge=0, le=10**12)
    output_microunits_per_million_tokens: int = Field(ge=0, le=10**12)


class ModelProviderProfile(AgentContractModel):
    """Git-reviewed model data-handling and structured-output authority."""

    schema_version: Literal["1.0"] = AGENT_RUN_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, max_length=128)
    version: SemVer
    title: str = Field(min_length=1, max_length=256)
    data_locality: Literal["local", "regional", "global"]
    # ``None`` means the provider publishes no exact maximum retention period.
    # It is intentionally different from zero-retention.
    retention_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    structured_output_protocol: Literal[
        "json_schema",
        "tool_calls",
        "json_object",
    ]
    context_window_tokens: int = Field(ge=1_024, le=10_000_000)
    max_output_tokens: int = Field(ge=64, le=1_000_000)
    permitted_data_classifications: list[DataClassification] = Field(
        min_length=1,
        max_length=4,
    )
    pricing: ModelPricing
    health_protocol: Literal["superlily-model-provider-v1"]

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if value != value.strip() or not _PROVIDER_ID_RE.fullmatch(value):
            raise ValueError("provider_id must be an exact opaque identifier")
        return value

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must contain visible text")
        return value

    @field_validator("permitted_data_classifications")
    @classmethod
    def validate_classifications(
        cls,
        value: list[DataClassification],
    ) -> list[DataClassification]:
        if len(value) != len(set(value)):
            raise ValueError("permitted_data_classifications must not contain duplicates")
        return value


class AgentBudget(AgentContractModel):
    max_model_attempts: int = Field(ge=1, le=8)
    max_model_turns: int = Field(ge=1, le=32)
    max_tool_proposals: int = Field(ge=0, le=32)
    max_tool_calls: int = Field(default=0, ge=0, le=8)
    max_sequential_depth: int = Field(default=0, ge=0, le=8)
    max_parallel_fanout: int = Field(default=0, ge=0, le=8)
    max_wall_time_ms: int = Field(ge=100, le=600_000)
    max_input_tokens: int = Field(ge=1, le=10_000_000)
    max_output_tokens: int = Field(ge=1, le=1_000_000)
    max_total_tokens: int = Field(ge=1, le=10_000_000)
    max_cost_microunits: int = Field(ge=0, le=10**12)
    max_input_bytes: int = Field(ge=1, le=1_048_576)
    max_output_bytes: int = Field(ge=1, le=1_048_576)
    max_result_bytes: int = Field(default=0, ge=0, le=1_048_576)
    max_artifact_bytes: int = Field(default=0, ge=0, le=10_485_760)

    @model_validator(mode="after")
    def validate_total_tokens(self) -> "AgentBudget":
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("max_total_tokens cannot exceed input plus output token budgets")
        if self.max_tool_calls == 0 and any(
            (
                self.max_sequential_depth,
                self.max_parallel_fanout,
                self.max_result_bytes,
                self.max_artifact_bytes,
            )
        ):
            raise ValueError("zero-call budgets cannot reserve tool result dimensions")
        if self.max_tool_calls > 0 and (
            self.max_sequential_depth == 0
            or self.max_parallel_fanout == 0
            or self.max_result_bytes == 0
        ):
            raise ValueError("tool-call budgets require depth, fanout, and result bounds")
        return self


class AgentPrincipalSnapshot(AgentContractModel):
    platform: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    sender_id: str = Field(min_length=1, max_length=256)
    conversation_key: str = Field(min_length=5, max_length=512)
    conversation_type: Literal["group", "private", "channel", "system"]
    observed_platform_roles: list[str] = Field(default_factory=list, max_length=32)
    source_event_id: str = Field(min_length=1, max_length=512)

    @field_validator("observed_platform_roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("observed_platform_roles must not contain duplicates")
        if any(not item or len(item) > 128 for item in value):
            raise ValueError("observed platform roles must be bounded non-empty strings")
        return value


class AgentContextMessage(AgentContractModel):
    source_event_id: str = Field(min_length=1, max_length=512)
    sender_id: str | None = Field(default=None, max_length=256)
    sender_name: str | None = Field(default=None, max_length=512)
    text: str = Field(max_length=8_192)
    occurred_at: AwareDatetime
    relation: Literal["current", "reply_target", "recent"]
    truncated: bool = False

    @field_validator("occurred_at", mode="before")
    @classmethod
    def validate_occurred_at(cls, value: object) -> object:
        return _parse_wire_aware_datetime(value)


class AgentToolInputFieldSummary(AgentContractModel):
    name: str = Field(min_length=1, max_length=128)
    json_type: Literal[
        "string",
        "number",
        "integer",
        "boolean",
        "object",
        "array",
        "null",
        "unknown",
    ]
    required: bool
    description: str | None = Field(default=None, max_length=512)


class AgentToolSummary(AgentContractModel):
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2_048)
    side_effect: Literal["none", "read", "compute", "write", "admin", "external_message"]
    permission: Literal["public", "conversation", "member", "moderator", "administrator"]
    input_schema_hash: Sha256
    input_fields: list[AgentToolInputFieldSummary] = Field(max_length=64)


class AgentContextSnapshot(AgentContractModel):
    schema_version: Literal["1.0"] = AGENT_RUN_SCHEMA_VERSION
    recipe_version: Literal["phase5-context-v1"] = AGENT_CONTEXT_RECIPE_VERSION
    policy_version: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    system_policy: str = Field(min_length=1, max_length=8_192)
    principal: AgentPrincipalSnapshot
    current_message: AgentContextMessage
    reply_graph: list[AgentContextMessage] = Field(max_length=16)
    recent_messages: list[AgentContextMessage] = Field(max_length=32)
    capabilities: list[str] = Field(max_length=64)
    eligible_tools: list[AgentToolSummary] = Field(max_length=64)
    data_classification: DataClassification
    retention_seconds: int = Field(ge=0, le=31_536_000)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must not contain duplicates")
        if any(not _IDENTIFIER_RE.fullmatch(item) for item in value):
            raise ValueError("capabilities must use bounded lowercase identifiers")
        return value

    @model_validator(mode="after")
    def validate_context_identity(self) -> "AgentContextSnapshot":
        if self.current_message.source_event_id != self.principal.source_event_id:
            raise ValueError("current message must match the principal source event")
        tool_keys = [
            (item.tool_id, item.descriptor_version, item.descriptor_hash)
            for item in self.eligible_tools
        ]
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("eligible tool summaries must be unique")
        try:
            canonicalize_json_value(self.model_dump(mode="json"))
        except CanonicalJSONError as exc:
            raise ValueError("context snapshot is outside the canonical JSON domain") from exc
        return self


class AgentRunCreateIn(AgentContractModel):
    schema_version: Literal["1.0"] = AGENT_RUN_SCHEMA_VERSION
    source_event_id: str = Field(min_length=1, max_length=512)
    model_provider_id: str = Field(min_length=1, max_length=128)
    model_profile_version: SemVer
    model_profile_hash: Sha256
    budget: AgentBudget

    @field_validator("model_provider_id")
    @classmethod
    def validate_model_provider_id(cls, value: str) -> str:
        if value != value.strip() or not _PROVIDER_ID_RE.fullmatch(value):
            raise ValueError("model_provider_id must be an exact opaque identifier")
        return value


class AgentToolPromotionIn(AgentContractModel):
    schema_version: Literal["1.0"] = AGENT_RUN_SCHEMA_VERSION
    proposal_id: str = Field(min_length=1, max_length=512)


class AgentToolProposal(AgentContractModel):
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    arguments: Any
    explanation: str = Field(min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def validate_arguments(self) -> "AgentToolProposal":
        try:
            canonicalize_json_value(self.arguments)
        except CanonicalJSONError as exc:
            raise ValueError("tool arguments are outside the canonical JSON domain") from exc
        return self


class AgentProposal(AgentContractModel):
    answer_markdown: str | None = Field(default=None, max_length=65_536)
    tool_proposals: list[AgentToolProposal] = Field(default_factory=list, max_length=32)
    uncertainty_basis_points: int = Field(ge=0, le=10_000)
    safe_summary: str = Field(min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def validate_content(self) -> "AgentProposal":
        if self.answer_markdown is None and not self.tool_proposals:
            raise ValueError("proposal must contain an answer or at least one tool proposal")
        return self


class AgentUsage(AgentContractModel):
    input_tokens: int = Field(ge=0, le=10_000_000)
    input_cache_hit_tokens: int = Field(ge=0, le=10_000_000)
    input_cache_miss_tokens: int = Field(ge=0, le=10_000_000)
    output_tokens: int = Field(ge=0, le=1_000_000)
    total_tokens: int = Field(ge=0, le=10_000_000)
    cost_microunits: int = Field(ge=0, le=10**12)
    input_bytes: int = Field(ge=0, le=1_048_576)
    output_bytes: int = Field(ge=0, le=1_048_576)
    wall_time_ms: int = Field(ge=0, le=600_000)

    @model_validator(mode="after")
    def validate_tokens(self) -> "AgentUsage":
        if (
            self.input_tokens
            != self.input_cache_hit_tokens + self.input_cache_miss_tokens
        ):
            raise ValueError(
                "input_tokens must equal cache-hit plus cache-miss input tokens"
            )
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens plus output_tokens")
        return self


class AgentAttemptReportIn(AgentContractModel):
    schema_version: Literal["1.0"] = AGENT_RUN_SCHEMA_VERSION
    outcome: AgentAttemptOutcome
    model_request_id: str | None = Field(default=None, max_length=256)
    raw_output_sha256: Sha256
    usage: AgentUsage
    proposal: AgentProposal | None = None
    safe_error_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    started_at: AwareDatetime
    completed_at: AwareDatetime

    @field_validator("started_at", "completed_at", mode="before")
    @classmethod
    def validate_wire_datetimes(cls, value: object) -> object:
        return _parse_wire_aware_datetime(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> "AgentAttemptReportIn":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        if self.outcome == "succeeded":
            if self.proposal is None or self.safe_error_code is not None:
                raise ValueError("successful attempts require a proposal and no error")
        elif self.proposal is not None or self.safe_error_code is None:
            raise ValueError("unsuccessful attempts require a safe error and no proposal")
        return self


def agent_context_hash(context: AgentContextSnapshot) -> str:
    return canonicalize_json_value(context.model_dump(mode="json")).sha256


def model_profile_hash(profile: ModelProviderProfile) -> str:
    return canonicalize_json_value(profile.model_dump(mode="json")).sha256


def agent_run_request_hash(
    payload: AgentRunCreateIn,
    *,
    creator_type: str,
    creator_id: str,
) -> str:
    return canonicalize_json_value(
        {
            "request": payload.model_dump(mode="json"),
            "creator_type": creator_type,
            "creator_id": creator_id,
        }
    ).sha256
