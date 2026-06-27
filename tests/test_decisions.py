from superlily_core.decisions import decide_event


def test_decision_routes_at_segment_to_talk() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        observation_bot_id="985393579",
        text="看看这个",
        segments=[
            {"type": "at", "data": {"qq": "985393579"}},
            {"type": "text", "data": {"text": "看看这个"}},
        ],
        attachments=[],
        metadata={},
        has_reply_link=False,
        reply_to_bot_response=False,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "mention_segment_targets_observer"
    assert decision.features["mentions_observing_bot"] is True


def test_decision_routes_reply_to_bot_response_to_talk() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        observation_bot_id="985393579",
        text="继续",
        segments=[{"type": "text", "data": {"text": "继续"}}],
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_to_bot_response=True,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "reply_to_bot_response"


def test_decision_routes_private_message_to_talk() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="private",
        observation_bot_id="985393579",
        text="你好",
        segments=[{"type": "text", "data": {"text": "你好"}}],
        attachments=[],
        metadata={},
        has_reply_link=False,
        reply_to_bot_response=False,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "private_message"
