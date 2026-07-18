"""第三阶段调用账本的共享线协议与状态机。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical_json import CanonicalJSONError, canonicalize_json_value
from .tool_registry import SemVer, Sha256, ToolId


TOOL_INVOCATION_SCHEMA_VERSION = "1.0"

InvocationCaller: TypeAlias = Literal["command", "admin_api"]
InvocationState: TypeAlias = Literal[
    "proposed",
    "rejected",
    "recorded_only",
    "awaiting_confirmation",
    "queued",
    "leased",
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancel_requested",
    "cancelled",
    "unknown_completion",
    "expired",
    "lease_expired",
]
InvocationTransitionEvent: TypeAlias = Literal[
    "propose",
    "reject",
    "record_only",
    "require_confirmation",
    "confirm",
    "confirmation_expire",
    "queue",
    "lease",
    "start",
    "complete_success",
    "complete_failure",
    "request_cancel",
    "cancel",
    "lease_expire",
    "timeout",
    "unknown_completion",
    "requeue",
]

TERMINAL_INVOCATION_STATES = frozenset(
    {
        "rejected",
        "recorded_only",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "unknown_completion",
        "expired",
    }
)

_LEGAL_INVOCATION_TRANSITIONS: dict[
    InvocationTransitionEvent,
    frozenset[tuple[InvocationState | None, InvocationState]],
] = {
    "propose": frozenset({(None, "proposed")}),
    "reject": frozenset(
        {
            ("proposed", "rejected"),
            ("awaiting_confirmation", "rejected"),
        }
    ),
    "record_only": frozenset({("proposed", "recorded_only")}),
    "require_confirmation": frozenset({("proposed", "awaiting_confirmation")}),
    "confirm": frozenset({("awaiting_confirmation", "queued")}),
    "confirmation_expire": frozenset({("awaiting_confirmation", "expired")}),
    "queue": frozenset({("proposed", "queued")}),
    "lease": frozenset({("queued", "leased")}),
    "start": frozenset({("leased", "running")}),
    "complete_success": frozenset({("running", "succeeded")}),
    "complete_failure": frozenset({("running", "failed")}),
    "request_cancel": frozenset(
        {
            ("leased", "cancel_requested"),
            ("running", "cancel_requested"),
        }
    ),
    "cancel": frozenset(
        {
            ("proposed", "cancelled"),
            ("awaiting_confirmation", "cancelled"),
            ("queued", "cancelled"),
            ("cancel_requested", "cancelled"),
        }
    ),
    "lease_expire": frozenset(
        {
            ("leased", "lease_expired"),
            ("running", "lease_expired"),
        }
    ),
    "timeout": frozenset(
        {
            ("queued", "timed_out"),
            ("leased", "timed_out"),
            ("running", "timed_out"),
            ("lease_expired", "timed_out"),
        }
    ),
    "unknown_completion": frozenset(
        {
            ("leased", "unknown_completion"),
            ("running", "unknown_completion"),
            ("cancel_requested", "unknown_completion"),
            ("lease_expired", "unknown_completion"),
        }
    ),
    "requeue": frozenset({("lease_expired", "queued")}),
}

_OPAQUE_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$"
OpaqueId: TypeAlias = Annotated[str, Field(pattern=_OPAQUE_ID)]


class InvocationContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=False,
        frozen=True,
    )


class InvocationPrincipal(InvocationContractModel):
    platform: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    sender_id: OpaqueId
    conversation_id: OpaqueId
    conversation_type: Literal["group", "private", "channel", "system"]
    platform_roles: list[OpaqueId] = Field(default_factory=list, max_length=32)
    source_event_id: str | None = Field(default=None, min_length=1, max_length=512)
    decision_id: OpaqueId | None = None
    claim_id: OpaqueId | None = None
    entry_id: OpaqueId | None = None

    @field_validator("platform_roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("platform_roles must not contain duplicates")
        return value

    @field_validator("source_event_id")
    @classmethod
    def validate_source_event_id(cls, value: str | None) -> str | None:
        if value is not None and value != value.strip():
            raise ValueError("source_event_id must not contain surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_conversation_identity(self) -> "InvocationPrincipal":
        expected_prefix = f"{self.conversation_type}:"
        if not self.conversation_id.startswith(expected_prefix):
            raise ValueError(
                "conversation_id must use the conversation_type:id canonical form"
            )
        if len(self.conversation_id) == len(expected_prefix):
            raise ValueError("conversation_id must contain an identifier after its type")
        return self


class ToolInvocationCreateIn(InvocationContractModel):
    schema_version: Literal["1.0"] = TOOL_INVOCATION_SCHEMA_VERSION
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    input: Any
    principal: InvocationPrincipal
    capabilities: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("capabilities must not contain duplicates")
        if any(
            not item
            or len(item) > 64
            or not item[0].islower()
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in item)
            for item in value
        ):
            raise ValueError("capabilities must use lowercase identifiers")
        return value

    @model_validator(mode="after")
    def validate_canonical_json_domain(self) -> "ToolInvocationCreateIn":
        try:
            canonicalize_json_value(self.input)
            canonicalize_json_value(self.principal.model_dump(mode="json"))
        except CanonicalJSONError as exc:
            raise ValueError("invocation request is outside the bounded canonical JSON domain") from exc
        return self


class ToolInvocationCancelIn(InvocationContractModel):
    schema_version: Literal["1.0"] = TOOL_INVOCATION_SCHEMA_VERSION
    reason: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cancellation reason must contain visible text")
        return value


def invocation_request_hash(
    payload: ToolInvocationCreateIn,
    *,
    caller: InvocationCaller,
    authenticated_subject: str,
) -> str:
    material = {
        "schema_version": TOOL_INVOCATION_SCHEMA_VERSION,
        "caller": caller,
        "authenticated_subject": authenticated_subject,
        "request": payload.model_dump(mode="json"),
    }
    return canonicalize_json_value(material).sha256


def legal_invocation_transitions() -> Mapping[
    InvocationTransitionEvent,
    frozenset[tuple[InvocationState | None, InvocationState]],
]:
    return _LEGAL_INVOCATION_TRANSITIONS


def validate_invocation_transition(
    previous_state: InvocationState | None,
    state: InvocationState,
    event: InvocationTransitionEvent,
) -> None:
    if (previous_state, state) not in _LEGAL_INVOCATION_TRANSITIONS[event]:
        raise ValueError(
            f"illegal invocation transition: event={event} from={previous_state} to={state}"
        )
