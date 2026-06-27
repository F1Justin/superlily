from dataclasses import dataclass
from typing import Any

POLICY_VERSION = "shadow-v1"
COMMAND_TARGET_INSTANCE = "lily-command"
TALK_TARGET_INSTANCE = "nekro-agent"

COMMAND_PREFIXES = (
    "/wf",
    "wf ",
    "wf\n",
    "/tex",
    "tex ",
    "tex\n",
    "/fortune",
    "/help",
    "/status",
    "/wordcloud",
)


@dataclass(frozen=True, slots=True)
class Decision:
    decision_type: str
    target_instance_id: str | None
    confidence: int
    reason: str
    features: dict[str, Any]


def _text_prefix(text: str) -> str | None:
    normalized = text.strip().lower()
    if not normalized:
        return None
    for prefix in COMMAND_PREFIXES:
        if normalized == prefix.strip() or normalized.startswith(prefix):
            return prefix.strip()
    return None


def _metadata_to_me(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("to_me") or metadata.get("is_tome"))


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
) -> Decision:
    command_prefix = _text_prefix(text or "")
    to_me = _metadata_to_me(metadata)
    mentions_observer = _mentions_observing_bot(segments, observation_bot_id)
    features = {
        "has_text": bool(text),
        "text_preview": (text or "")[:200],
        "has_attachments": bool(attachments),
        "conversation_type": conversation_type,
        "command_prefix": command_prefix,
        "to_me": to_me,
        "mentions_observing_bot": mentions_observer,
        "has_reply_link": has_reply_link,
        "reply_to_bot_response": reply_to_bot_response,
    }

    if source_event_type != "message":
        return Decision("ignore", None, 95, "non_message_event", features)

    if command_prefix:
        return Decision(
            "command",
            COMMAND_TARGET_INSTANCE,
            95,
            f"command_prefix:{command_prefix}",
            features,
        )

    if to_me:
        return Decision("talk", TALK_TARGET_INSTANCE, 90, "addressed_to_bot", features)

    if mentions_observer:
        return Decision("talk", TALK_TARGET_INSTANCE, 85, "mention_segment_targets_observer", features)

    if reply_to_bot_response:
        return Decision("talk", TALK_TARGET_INSTANCE, 80, "reply_to_bot_response", features)

    if conversation_type == "private":
        return Decision("talk", TALK_TARGET_INSTANCE, 75, "private_message", features)

    if has_reply_link:
        return Decision("observe_only", None, 60, "reply_reference_observed", features)

    return Decision("observe_only", None, 70, "ordinary_message", features)
