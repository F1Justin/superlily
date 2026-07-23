"""Deterministic constrained adapter used to prove cross-platform rendering."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .canonical_json import canonicalize_json_value
from .delivery_planning import DeliveryPlanDecision, plan_render_delivery
from .rendering import RenderDocument, render_document_plain_text


CONSTRAINED_TEXT_PROFILE: dict[str, Any] = {
    "profile": "constrained_text.v1",
    "supported": ["send_text"],
    "limits": {
        "max_payloads": 1,
        "max_text_chars": 8_000,
    },
}


class AdapterSimulationError(ValueError):
    def __init__(self, code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class AdapterSimulationReceipt:
    profile: str
    capability_hash: str
    decision_hash: str
    semantic_text_sha256: str
    transcript: tuple[dict[str, Any], ...]
    transcript_hash: str

    def as_json(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "capability_hash": self.capability_hash,
            "decision_hash": self.decision_hash,
            "semantic_text_sha256": self.semantic_text_sha256,
            "transcript": list(self.transcript),
            "transcript_hash": self.transcript_hash,
        }


def simulate_adapter_delivery(
    document: RenderDocument,
    capability_profile: dict[str, Any],
    *,
    rendered_artifact_sha256: str | None = None,
) -> tuple[DeliveryPlanDecision, AdapterSimulationReceipt]:
    """Produce a bounded, repeatable send transcript without platform access."""

    decision = plan_render_delivery(document, capability_profile)
    limits = decision.capability_snapshot["limits"]
    max_payloads = limits.get("max_payloads", 8)
    if (
        not isinstance(max_payloads, int)
        or isinstance(max_payloads, bool)
        or not 1 <= max_payloads <= 16
    ):
        raise AdapterSimulationError(
            "invalid_adapter_limit", "adapter max_payloads limit is invalid"
        )
    if len(decision.ordered_payloads) > max_payloads:
        raise AdapterSimulationError(
            "adapter_payload_limit_exceeded",
            "delivery plan exceeds the adapter payload limit",
        )

    transcript: list[dict[str, Any]] = []
    for payload in decision.ordered_payloads:
        if payload.family == "text":
            assert decision.fallback_text is not None
            max_text_chars = limits.get("max_text_chars", 8_000)
            if (
                not isinstance(max_text_chars, int)
                or isinstance(max_text_chars, bool)
                or not 1 <= max_text_chars <= 32_000
            ):
                raise AdapterSimulationError(
                    "invalid_adapter_limit", "adapter max_text_chars limit is invalid"
                )
            if len(decision.fallback_text) > max_text_chars:
                raise AdapterSimulationError(
                    "adapter_text_limit_exceeded",
                    "delivery text exceeds the adapter character limit",
                )
            transcript.append(
                {
                    "position": payload.position,
                    "operation": "send_text",
                    "text": decision.fallback_text,
                }
            )
        else:
            if rendered_artifact_sha256 is None:
                raise AdapterSimulationError(
                    "render_artifact_required",
                    "image delivery simulation requires an artifact digest",
                )
            transcript.append(
                {
                    "position": payload.position,
                    "operation": "send_image",
                    "content_sha256": rendered_artifact_sha256,
                }
            )

    semantic_text = render_document_plain_text(decision.resolved_document)
    canonical = canonicalize_json_value(transcript)
    return decision, AdapterSimulationReceipt(
        profile=decision.capability_snapshot["profile"],
        capability_hash=decision.capability_hash,
        decision_hash=decision.decision_hash,
        semantic_text_sha256=sha256(semantic_text.encode("utf-8")).hexdigest(),
        transcript=tuple(transcript),
        transcript_hash=canonical.sha256,
    )


def simulate_constrained_text_delivery(
    document: RenderDocument,
) -> tuple[DeliveryPlanDecision, AdapterSimulationReceipt]:
    return simulate_adapter_delivery(document, CONSTRAINED_TEXT_PROFILE)
