from dataclasses import dataclass
from typing import Any

from .command_registry import CommandRegistry

POLICY_VERSION = "shadow-v1"
TALK_TARGET_INSTANCE = "nekro-agent"


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


def _segment_data(segment: dict[str, Any]) -> dict[str, Any]:
    data = segment.get("data")
    if isinstance(data, dict):
        return data
    return segment


def _mentions_observing_bot(segments: list[dict[str, Any]], bot_id: str) -> bool:
    for segment in segments:
        if segment.get("type") != "at":
            continue
        data = _segment_data(segment)
        target = data.get("qq") or data.get("target") or data.get("target_platform_userid")
        if str(target) == str(bot_id):
            return True
    return False


def decide_event(
    *,
    source_event_type: str,
    conversation_type: str,
    observation_bot_id: str,
    text: str | None,
    segments: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    metadata: dict[str, Any],
    has_reply_link: bool,
    reply_to_bot_response: bool,
    command_registry: CommandRegistry | None = None,
    command_registry_error: str | None = None,
) -> Decision:
    command_match = command_registry.match(text) if command_registry else None
    bridge_to_me = _metadata_to_me(metadata)
    summons_talk_bot = _summons_talk_bot(text)
    mentions_observer = _mentions_observing_bot(segments, observation_bot_id)
    features = {
        "has_text": bool(text),
        "text_preview": (text or "")[:200],
        "has_attachments": bool(attachments),
        "conversation_type": conversation_type,
        "command_registry_version": command_registry.version if command_registry else None,
        "command_registry_error": command_registry_error,
        "command_prefix": command_match.trigger if command_match and command_match.kind == "prefix" else None,
        "matched_command": command_match.as_feature() if command_match else None,
        "to_me": summons_talk_bot,
        "bridge_to_me": bridge_to_me,
        "summons_talk_bot": summons_talk_bot,
        "mentions_observing_bot": mentions_observer,
        "has_reply_link": has_reply_link,
        "reply_to_bot_response": reply_to_bot_response,
    }

    if source_event_type != "message":
        return Decision("ignore", None, 95, "non_message_event", features)

    if command_match:
        reason = f"command_{command_match.kind}:{command_match.trigger}"
        return Decision(
            "command",
            command_match.target_instance_id,
            command_match.confidence,
            reason,
            features,
        )

    if summons_talk_bot:
        return Decision("talk", TALK_TARGET_INSTANCE, 90, "summons_talk_bot", features)

    if mentions_observer:
        return Decision("talk", TALK_TARGET_INSTANCE, 85, "mention_segment_targets_observer", features)

    if reply_to_bot_response:
        return Decision("talk", TALK_TARGET_INSTANCE, 80, "reply_to_bot_response", features)

    if conversation_type == "private":
        return Decision("talk", TALK_TARGET_INSTANCE, 75, "private_message", features)

    if has_reply_link:
        return Decision("observe_only", None, 60, "reply_reference_observed", features)

    return Decision("observe_only", None, 70, "ordinary_message", features)
