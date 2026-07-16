from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

from .command_registry import CommandRegistry

POLICY_VERSION = "qq-v3-policy-v5"
TALK_TARGET_INSTANCE = "nekro-agent"
COMMAND_TARGET_INSTANCE = "lily-command"
TALK_ENABLED_MODES = {"conversation_only", "full"}
COMMAND_ENABLED_MODES = {"command_only", "full"}


@dataclass(frozen=True, slots=True)
class Decision:
    decision_type: str
    target_instance_id: str | None
    confidence: int
    reason: str
    features: dict[str, Any]


def _metadata_to_me(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("to_me") or metadata.get("is_tome"))


def _summons_talk_bot(text: str | None) -> bool:
    return "莉莉" in (text or "")


def decide_event(
    *,
    source_event_type: str,
    conversation_type: str,
    text: str | None,
    attachments: list[dict[str, Any]],
    metadata: dict[str, Any],
    has_reply_link: bool,
    reply_target_instance_id: str | None = None,
    reply_target_status: str = "none",
    mentioned_bot_instance_ids: Sequence[str] = (),
    observation_count: int = 1,
    command_registry: CommandRegistry | None = None,
    command_registry_error: str | None = None,
    command_registry_runtime: dict[str, Any] | None = None,
    sender_bot_instance_id: str | None = None,
    conversation_mode: str = "full",
    command_eligible: bool = True,
    observing_instance_id: str | None = None,
) -> Decision:
    bot_message = sender_bot_instance_id is not None
    command_match = (
        command_registry.match(text)
        if command_registry and not bot_message and command_eligible
        else None
    )
    bridge_to_me = _metadata_to_me(metadata)
    summons_talk_bot = _summons_talk_bot(text) if not bot_message else False
    mentioned_instances = sorted(set(mentioned_bot_instance_ids))
    mentions_known_bot = bool(mentioned_instances)
    features = {
        "has_text": bool(text),
        "text_preview": (text or "")[:200],
        "has_attachments": bool(attachments),
        "conversation_type": conversation_type,
        "conversation_mode": conversation_mode,
        "command_eligible": command_eligible,
        "command_registry_version": command_registry.version if command_registry else None,
        "command_registry_error": command_registry_error,
        "command_registry_runtime": command_registry_runtime,
        "command_prefix": (
            command_match.trigger if command_match and command_match.kind in {"command", "prefix"} else None
        ),
        "matched_command": command_match.as_feature() if command_match else None,
        "to_me": summons_talk_bot,
        "bridge_to_me": bridge_to_me,
        "summons_talk_bot": summons_talk_bot,
        "mentions_observing_bot": mentions_known_bot,
        "mentioned_bot_instance_ids": mentioned_instances,
        "has_reply_link": has_reply_link,
        "reply_to_bot_response": reply_target_instance_id is not None,
        "reply_target_instance_id": reply_target_instance_id,
        "reply_target_status": reply_target_status,
        "observation_count": observation_count,
        "sender_bot_instance_id": sender_bot_instance_id,
        "observing_instance_id": observing_instance_id,
    }

    if source_event_type != "message":
        return Decision("ignore", None, 95, "non_message_event", features)

    if bot_message:
        return Decision("observe_only", None, 100, "bot_message_observed", features)

    if has_reply_link:
        if reply_target_instance_id == TALK_TARGET_INSTANCE:
            if conversation_mode not in TALK_ENABLED_MODES:
                return Decision(
                    "observe_only",
                    None,
                    95,
                    f"conversation_mode_{conversation_mode}",
                    features,
                )
            return Decision("talk", TALK_TARGET_INSTANCE, 95, "reply_to_talk_response", features)
        if reply_target_instance_id == COMMAND_TARGET_INSTANCE:
            return Decision("observe_only", None, 90, "reply_to_command_response_observed", features)
        if reply_target_status in {"ambiguous", "conflict"}:
            return Decision("observe_only", None, 50, "reply_target_conflict_observed", features)
        if summons_talk_bot or mentions_known_bot:
            if conversation_mode not in TALK_ENABLED_MODES:
                return Decision(
                    "observe_only",
                    None,
                    90,
                    f"conversation_mode_{conversation_mode}",
                    features,
                )
            return Decision("talk", TALK_TARGET_INSTANCE, 90, "summons_talk_bot_with_reply", features)
        if reply_target_status == "resolved_other":
            return Decision("observe_only", None, 75, "reply_to_other_observed", features)
        return Decision("observe_only", None, 60, "reply_reference_observed", features)

    if command_match and (
        conversation_type != "private"
        or command_match.target_instance_id == observing_instance_id
    ):
        if conversation_type == "group" and conversation_mode not in COMMAND_ENABLED_MODES:
            return Decision("observe_only", None, 95, "command_target_unavailable", features)
        reason_kind = "prefix" if command_match.kind == "command" else command_match.kind
        reason = f"command_{reason_kind}:{command_match.trigger}"
        return Decision(
            "command",
            command_match.target_instance_id,
            command_match.confidence,
            reason,
            features,
        )

    if conversation_type == "group" and conversation_mode in {"command_only", "observe_only"}:
        return Decision("observe_only", None, 90, f"conversation_mode_{conversation_mode}", features)

    # QQ private messages are addressed to one concrete account and cannot be
    # handed to a different bot account.  Lily handles commands received by
    # Lily; Nekro handles conversations received by Nekro.  Other observer or
    # standby accounts remain silent until an explicit HA role transition.
    if conversation_type == "private":
        if observing_instance_id in {None, TALK_TARGET_INSTANCE}:
            return Decision("talk", TALK_TARGET_INSTANCE, 95, "private_recipient_talk", features)
        return Decision("observe_only", None, 95, "private_recipient_observed", features)

    if summons_talk_bot:
        return Decision("talk", TALK_TARGET_INSTANCE, 90, "summons_talk_bot", features)

    if mentions_known_bot:
        return Decision("talk", TALK_TARGET_INSTANCE, 85, "explicit_bot_mention", features)

    return Decision("observe_only", None, 70, "ordinary_message", features)
