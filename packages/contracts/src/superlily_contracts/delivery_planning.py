"""Deterministic capability planning for RenderDocument delivery.

The planner is deliberately platform-neutral.  It consumes only a validated
``RenderDocument`` and a bounded capability snapshot, resolves presentation
alternatives, and emits an auditable decision.  It never sends a platform
message and never dereferences an artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from typing import Any, Literal

from .canonical_json import canonicalize_json_value
from .rendering import (
    AlternativeBlock,
    GroupBlock,
    RenderBlock,
    RenderDocument,
    render_document_hash,
    render_document_plain_text,
)


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class CapabilityPlanningError(ValueError):
    """A bounded capability profile cannot deliver the document."""

    def __init__(self, code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class AlternativeSelection:
    node_id: str
    preferred_option_id: str
    selected_option_id: str

    def as_json(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "preferred_option_id": self.preferred_option_id,
            "selected_option_id": self.selected_option_id,
        }


@dataclass(frozen=True, slots=True)
class RejectedAlternative:
    node_id: str
    option_id: str
    reason: Literal["missing_capabilities", "lower_priority"]
    missing_capabilities: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "option_id": self.option_id,
            "reason": self.reason,
            "missing_capabilities": list(self.missing_capabilities),
        }


@dataclass(frozen=True, slots=True)
class PlannedPayload:
    position: int
    family: Literal["image", "text"]
    source: Literal["render_artifact", "fallback_text"]
    content_sha256: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "family": self.family,
            "source": self.source,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class DeliveryPlanDecision:
    capability_snapshot: dict[str, Any]
    capability_hash: str
    selected_family: Literal["image", "text"]
    fallback_text: str | None
    selected_alternatives: tuple[AlternativeSelection, ...]
    rejected_alternatives: tuple[RejectedAlternative, ...]
    degradation_reasons: tuple[str, ...]
    ordered_payloads: tuple[PlannedPayload, ...]
    resolved_document: RenderDocument
    resolved_document_hash: str
    decision_hash: str

    def audit_json(self) -> dict[str, Any]:
        return {
            "capability_snapshot": self.capability_snapshot,
            "capability_hash": self.capability_hash,
            "selected_family": self.selected_family,
            "fallback_text": self.fallback_text,
            "selected_alternatives": [
                item.as_json() for item in self.selected_alternatives
            ],
            "rejected_alternatives": [
                item.as_json() for item in self.rejected_alternatives
            ],
            "degradation_reasons": list(self.degradation_reasons),
            "ordered_payloads": [item.as_json() for item in self.ordered_payloads],
            "resolved_document_hash": self.resolved_document_hash,
        }


def normalize_capability_snapshot(raw: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Validate and canonicalize the small adapter capability input."""

    if not isinstance(raw, dict):
        raise CapabilityPlanningError(
            "invalid_capability_profile", "capability snapshot must be an object"
        )
    profile = raw.get("profile", "unknown")
    if not isinstance(profile, str) or not _PROFILE_RE.fullmatch(profile):
        raise CapabilityPlanningError(
            "invalid_capability_profile", "capability profile identifier is invalid"
        )
    supported = raw.get("supported", [])
    if (
        not isinstance(supported, list)
        or len(supported) > 64
        or any(
            not isinstance(item, str) or not _CAPABILITY_RE.fullmatch(item)
            for item in supported
        )
    ):
        raise CapabilityPlanningError(
            "invalid_capability_profile", "capability list is invalid"
        )
    limits = raw.get("limits", {})
    if not isinstance(limits, dict):
        raise CapabilityPlanningError(
            "invalid_capability_profile", "capability limits must be an object"
        )
    normalized = {
        "profile": profile,
        "supported": sorted(set(supported)),
        "limits": limits,
    }
    canonical = canonicalize_json_value(normalized)
    if len(canonical.canonical_bytes) > 8_192:
        raise CapabilityPlanningError(
            "invalid_capability_profile", "capability snapshot exceeds its byte limit"
        )
    return normalized, canonical.sha256


def _resolve_alternatives(
    document: RenderDocument,
    supported: frozenset[str],
) -> tuple[
    RenderDocument,
    tuple[AlternativeSelection, ...],
    tuple[RejectedAlternative, ...],
    tuple[str, ...],
]:
    blocks: list[RenderBlock] = []
    selected: list[AlternativeSelection] = []
    rejected: list[RejectedAlternative] = []
    degradation: list[str] = []
    alternative_index = 0
    for block in document.blocks:
        if not isinstance(block, AlternativeBlock):
            blocks.append(block)
            continue
        alternative_index += 1
        node_id = block.node_id or f"alternative-{alternative_index}"
        compatible = [
            option
            for option in block.options
            if set(option.requires).issubset(supported)
        ]
        if not compatible:
            raise CapabilityPlanningError(
                "document_alternative_unavailable",
                "no document alternative matches the adapter capability profile",
            )
        preferred = next(
            option
            for option in block.options
            if option.option_id == block.preferred_option_id
        )
        chosen = preferred if preferred in compatible else compatible[0]
        selected.append(
            AlternativeSelection(
                node_id=node_id,
                preferred_option_id=block.preferred_option_id,
                selected_option_id=chosen.option_id,
            )
        )
        for option in block.options:
            if option.option_id == chosen.option_id:
                continue
            missing = tuple(sorted(set(option.requires) - supported))
            rejected.append(
                RejectedAlternative(
                    node_id=node_id,
                    option_id=option.option_id,
                    reason="missing_capabilities" if missing else "lower_priority",
                    missing_capabilities=missing,
                )
            )
        if chosen.option_id != block.preferred_option_id:
            degradation.append(
                "alternative_fallback:"
                f"{node_id}:{block.preferred_option_id}->{chosen.option_id}"
            )
        blocks.append(
            GroupBlock(
                node_id=block.node_id,
                accessibility_text=block.accessibility_text,
                blocks=chosen.blocks,
            )
        )
    resolved = document.model_copy(update={"blocks": blocks})
    return resolved, tuple(selected), tuple(rejected), tuple(degradation)


def plan_render_delivery(
    document: RenderDocument,
    capability_snapshot: dict[str, Any],
) -> DeliveryPlanDecision:
    """Resolve one deterministic delivery decision for an adapter profile."""

    normalized, capability_hash = normalize_capability_snapshot(capability_snapshot)
    supported = frozenset(normalized["supported"])
    resolved, selected, rejected, alternative_degradation = _resolve_alternatives(
        document, supported
    )

    if "send_image" in supported:
        selected_family: Literal["image", "text"] = "image"
        fallback_text = None
        degradation = alternative_degradation
        payloads = (
            PlannedPayload(
                position=0,
                family="image",
                source="render_artifact",
            ),
        )
    elif "send_text" in supported:
        selected_family = "text"
        fallback_text = render_document_plain_text(resolved)
        degradation = (
            *alternative_degradation,
            "image_unsupported_fallback_to_text",
        )
        payloads = (
            PlannedPayload(
                position=0,
                family="text",
                source="fallback_text",
                content_sha256=sha256(fallback_text.encode("utf-8")).hexdigest(),
            ),
        )
    else:
        raise CapabilityPlanningError(
            "delivery_capability_unavailable",
            "adapter cannot deliver the rendered document",
        )

    resolved_hash = render_document_hash(resolved)
    decision_material = {
        "capability_snapshot": normalized,
        "capability_hash": capability_hash,
        "selected_family": selected_family,
        "fallback_text": fallback_text,
        "selected_alternatives": [item.as_json() for item in selected],
        "rejected_alternatives": [item.as_json() for item in rejected],
        "degradation_reasons": list(degradation),
        "ordered_payloads": [item.as_json() for item in payloads],
        "resolved_document_hash": resolved_hash,
    }
    decision_hash = canonicalize_json_value(decision_material).sha256
    return DeliveryPlanDecision(
        capability_snapshot=normalized,
        capability_hash=capability_hash,
        selected_family=selected_family,
        fallback_text=fallback_text,
        selected_alternatives=selected,
        rejected_alternatives=rejected,
        degradation_reasons=tuple(degradation),
        ordered_payloads=payloads,
        resolved_document=resolved,
        resolved_document_hash=resolved_hash,
        decision_hash=decision_hash,
    )
