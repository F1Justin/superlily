from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .correlation import CORRELATION_VERSION


ACTIONABLE_DECISIONS = {"command", "talk"}
UNSAFE_REPLY_STATUSES = {"unresolved", "ambiguous", "conflict"}
SUPPRESS_ALL_DECISION_REASONS = {"reply_to_other_observed"}


@dataclass(frozen=True, slots=True)
class ClaimEvaluation:
    action: str
    reason: str
    ready: bool
    gates: dict[str, Any]


def evaluate_claim(
    *,
    mode: str,
    requesting_instance_id: str,
    decision_type: str,
    decision_reason: str,
    target_instance_id: str | None,
    confidence: int,
    decision_features: dict[str, Any],
    correlation_version: str | None,
    observation_count: int,
    required_observations: int,
    minimum_confidence: int,
    target_status: str | None,
) -> ClaimEvaluation:
    runtime = decision_features.get("command_registry_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    reply_status = str(decision_features.get("reply_target_status") or "none")
    suppress_all = (
        decision_type == "observe_only"
        and decision_reason in SUPPRESS_ALL_DECISION_REASONS
        and target_instance_id is None
        and reply_status == "resolved_other"
        and decision_features.get("summons_talk_bot") is False
        and decision_features.get("mentions_observing_bot") is False
    )
    effective_required_observations = 1 if suppress_all else required_observations
    gates = {
        "mode": mode,
        "decision_type": decision_type,
        "decision_reason": decision_reason,
        "target_instance_id": target_instance_id,
        "suppression_scope": "all_instances" if suppress_all else None,
        "correlation_version": correlation_version,
        "observation_count": observation_count,
        "required_observations": required_observations,
        "effective_required_observations": effective_required_observations,
        "confidence": confidence,
        "minimum_confidence": minimum_confidence,
        "registry_error": decision_features.get("command_registry_error"),
        "registry_runtime_status": runtime.get("status"),
        "runtime_match": runtime.get("runtime_match"),
        "unregistered_runtime_match": runtime.get("unregistered_match"),
        "reply_target_status": reply_status,
        "target_status": target_status,
    }

    if mode == "off":
        return ClaimEvaluation("abstain", "claim_mode_off", False, gates)
    if not suppress_all and (decision_type not in ACTIONABLE_DECISIONS or not target_instance_id):
        return ClaimEvaluation("abstain", "non_actionable_decision", False, gates)
    if correlation_version != CORRELATION_VERSION:
        return ClaimEvaluation("abstain", "strong_correlation_required", False, gates)
    if observation_count < effective_required_observations:
        return ClaimEvaluation("abstain", "insufficient_observations", False, gates)
    if confidence < minimum_confidence:
        return ClaimEvaluation("abstain", "confidence_below_threshold", False, gates)
    if suppress_all:
        return ClaimEvaluation(
            "deny",
            f"decision_suppress_all:{decision_reason}",
            True,
            gates,
        )
    if decision_features.get("command_registry_error"):
        return ClaimEvaluation("abstain", "command_registry_unavailable", False, gates)
    if runtime.get("status") != "fresh":
        return ClaimEvaluation("abstain", "runtime_registry_not_fresh", False, gates)
    # Runtime command introspection is a safety gate for command ownership.
    # Once the canonical policy chooses talk (notably a reply to Nekro), an
    # unregistered Lily matcher is conflicting local behavior that coordination
    # must suppress, not a reason to let both bots fail open and respond.
    if decision_type == "command" and runtime.get("unregistered_match") is not None:
        return ClaimEvaluation("abstain", "unregistered_runtime_command", False, gates)
    if decision_type == "command" and runtime.get("runtime_match") is None:
        return ClaimEvaluation("abstain", "command_not_confirmed_at_runtime", False, gates)
    if decision_type == "command" and runtime.get("runtime_match", {}).get("complete") is not True:
        matched_command = decision_features.get("matched_command")
        if not isinstance(matched_command, dict) or matched_command.get("runtime_introspection") != "reviewed":
            return ClaimEvaluation("abstain", "runtime_match_not_fully_introspected", False, gates)
    if decision_type == "command":
        matched_command = decision_features.get("matched_command")
        if not isinstance(matched_command, dict):
            return ClaimEvaluation("abstain", "command_metadata_missing", False, gates)
        if matched_command.get("sensitive") is True:
            return ClaimEvaluation("abstain", "sensitive_command_not_enforced", False, gates)
        if matched_command.get("permission") != "public":
            return ClaimEvaluation("abstain", "command_permission_not_modeled", False, gates)
    if reply_status in UNSAFE_REPLY_STATUSES:
        return ClaimEvaluation("abstain", "reply_target_not_deterministic", False, gates)
    if target_status != "online":
        return ClaimEvaluation("abstain", "target_instance_not_online", False, gates)

    action = "allow" if requesting_instance_id == target_instance_id else "deny"
    return ClaimEvaluation(action, f"decision_target:{target_instance_id}", True, gates)


def enforcement_enabled(
    *,
    mode: str,
    platform: str,
    conversation_type: str,
    conversation_id: str,
    canary_conversations: frozenset[str],
) -> bool:
    if mode == "enforce":
        return True
    if mode != "canary":
        return False
    key = f"{platform}:{conversation_type}:{conversation_id}"
    return key in canary_conversations
