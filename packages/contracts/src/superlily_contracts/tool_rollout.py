"""第三阶段 Git-bound 精确发布计划合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .canonical_json import CanonicalJSON, CanonicalJSONError, canonicalize_json
from .tool_registry import AuthorityModel, ToolRegistryContractError


TOOL_ROLLOUT_SCHEMA_VERSION = "1.0"

ToolId: TypeAlias = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"),
]
SemVer: TypeAlias = Annotated[
    str,
    Field(
        pattern=(
            r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$"
        )
    ),
]
Sha256: TypeAlias = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_PLAN_ID_RE = re.compile(r"^[a-z][a-z0-9.-]{2,127}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


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


class ToolRolloutPlanItem(AuthorityModel):
    """一个没有通配符、且只选择一个 Provider 的执行目标。"""

    item_id: str = Field(min_length=3, max_length=128)
    tool_id: ToolId
    descriptor_version: SemVer
    descriptor_hash: Sha256
    canonical_conversation: str = Field(min_length=5, max_length=512)
    caller: Literal["command", "agent", "admin_api"]
    provider_id: str = Field(min_length=1, max_length=128)
    expected_descriptor_resource_version: int = Field(ge=1, le=2_147_483_647)
    expected_provider_resource_version: int = Field(ge=1, le=2_147_483_647)

    @field_validator("item_id")
    @classmethod
    def validate_item_id(cls, value: str) -> str:
        if not _PLAN_ID_RE.fullmatch(value):
            raise ValueError("item_id must be an exact lowercase identifier")
        return value

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str) -> str:
        if value != value.strip() or not _PROVIDER_ID_RE.fullmatch(value):
            raise ValueError("provider_id must be an exact opaque identifier")
        return value

    @field_validator("canonical_conversation")
    @classmethod
    def validate_conversation(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("canonical_conversation must be exact")
        parts = value.split(":", 2)
        if (
            len(parts) != 3
            or not _PLATFORM_RE.fullmatch(parts[0])
            or parts[1] not in {"group", "private", "channel", "system"}
            or not parts[2]
            or len(parts[2]) > 384
        ):
            raise ValueError("canonical_conversation must use platform:type:id")
        return value


class ToolRolloutPlan(AuthorityModel):
    """M3 首包只允许有硬过期和调用上限的 canary 计划。"""

    schema_version: Literal["1.0"] = TOOL_ROLLOUT_SCHEMA_VERSION
    plan_id: str = Field(min_length=3, max_length=128)
    version: SemVer
    mode: Literal["canary"]
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    max_invocations: int = Field(ge=1, le=1_000)
    rollback_mode: Literal["ledger_only"]
    reason: str = Field(min_length=8, max_length=512)
    items: list[ToolRolloutPlanItem] = Field(min_length=1, max_length=32)

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        if not _PLAN_ID_RE.fullmatch(value):
            raise ValueError("plan_id must be an exact lowercase identifier")
        return value

    @field_validator("starts_at", "expires_at", mode="before")
    @classmethod
    def validate_datetime(cls, value: Any) -> Any:
        return _wire_aware_datetime(value)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("reason must not contain surrounding whitespace")
        return value

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[ToolRolloutPlanItem]) -> list[ToolRolloutPlanItem]:
        item_ids = [item.item_id for item in value]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("rollout plan item IDs must be unique")
        targets = [
            (
                item.tool_id,
                item.descriptor_version,
                item.descriptor_hash,
                item.canonical_conversation,
                item.caller,
            )
            for item in value
        ]
        if len(targets) != len(set(targets)):
            raise ValueError("rollout plan must select exactly one provider per execution target")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "ToolRolloutPlan":
        if self.expires_at <= self.starts_at:
            raise ValueError("rollout plan expires_at must be after starts_at")
        if self.expires_at - self.starts_at > timedelta(hours=24):
            raise ValueError("rollout plan lifetime must not exceed 24 hours")
        return self


@dataclass(frozen=True, slots=True)
class LoadedToolRolloutPlan:
    plan: ToolRolloutPlan
    authority: CanonicalJSON


def load_tool_rollout_plan(source: bytes) -> LoadedToolRolloutPlan:
    """严格加载、验证并内容寻址一个 reviewed rollout plan。"""

    try:
        authority = canonicalize_json(source)
        if not isinstance(authority.value, dict):
            raise ToolRegistryContractError("tool rollout plan root must be an object")
        plan = ToolRolloutPlan.model_validate(authority.value)
    except ToolRegistryContractError:
        raise
    except (CanonicalJSONError, ValueError) as exc:
        raise ToolRegistryContractError("tool rollout plan authority document is invalid") from exc
    return LoadedToolRolloutPlan(plan=plan, authority=authority)
