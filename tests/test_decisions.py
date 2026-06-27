import pytest

from superlily_core.command_registry import CommandRegistry, CommandRule, load_command_registry
from superlily_core.decisions import decide_event


def test_decision_routes_registered_prefix_command_to_lily() -> None:
    registry = CommandRegistry(
        version="test",
        rules=(
            CommandRule(
                id="test.wolfram",
                kind="prefix",
                triggers=("wf",),
                target_instance_id="lily-command",
                source_plugin="plugins.wolfram",
            ),
        ),
    )
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        observation_bot_id="985393579",
        text="wf 1+1",
        segments=[{"type": "text", "data": {"text": "wf 1+1"}}],
        attachments=[],
        metadata={"to_me": True},
        has_reply_link=False,
        reply_to_bot_response=False,
        command_registry=registry,
    )

    assert decision.decision_type == "command"
    assert decision.target_instance_id == "lily-command"
    assert decision.reason == "command_prefix:wf"
    assert decision.features["command_registry_version"] == "test"
    assert decision.features["command_prefix"] == "wf"
    assert decision.features["matched_command"] == {
        "rule_id": "test.wolfram",
        "kind": "prefix",
        "trigger": "wf",
        "target_instance_id": "lily-command",
        "source_plugin": "plugins.wolfram",
        "confidence": 95,
        "permission": "public",
        "sensitive": False,
        "description": "",
    }


def test_command_registry_matches_exact_and_regex_without_broad_prefix_false_positive() -> None:
    registry = CommandRegistry(
        version="test",
        rules=(
            CommandRule(
                id="test.fortune",
                kind="exact",
                triggers=("fortune",),
                target_instance_id="lily-command",
                source_plugin="plugins.touhou_tarots",
            ),
            CommandRule(
                id="test.waifu",
                kind="regex",
                triggers=(r"^设置换老婆次数\s*\d+$",),
                target_instance_id="lily-command",
                source_plugin="nonebot_plugin_today_waifu",
                permission="group_admin",
            ),
            CommandRule(
                id="test.status",
                kind="prefix",
                triggers=("status",),
                target_instance_id="lily-command",
                source_plugin="nonebot_plugin_picstatus",
            ),
        ),
    )

    assert registry.match("fortune").rule_id == "test.fortune"
    assert registry.match("fortune cookie") is None
    assert registry.match("设置换老婆次数 3").permission == "group_admin"
    assert registry.match("status now").rule_id == "test.status"
    assert registry.match("statusquo") is None


def test_default_registry_covers_known_external_commands() -> None:
    registry = load_command_registry()

    train = registry.match("查询列车 G1")
    waifu = registry.match("今日老婆")
    sensitive = registry.match("重启nb")

    assert train is not None
    assert train.source_plugin == "nonebot_plugin_cnrail"
    assert waifu is not None
    assert waifu.source_plugin == "nonebot_plugin_today_waifu"
    assert sensitive is not None
    assert sensitive.permission == "superuser"
    assert sensitive.sensitive is True


def test_command_registry_rejects_unknown_permission(tmp_path) -> None:
    registry_path = tmp_path / "bad.toml"
    registry_path.write_text(
        """
version = "bad"

[[rules]]
id = "bad.permission"
kind = "prefix"
triggers = ["bad"]
permission = "owner"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported permission"):
        load_command_registry(registry_path)


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


def test_bridge_to_me_does_not_override_core_summon_policy() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        observation_bot_id="985393579",
        text="Lily Sol",
        segments=[{"type": "text", "data": {"text": "Lily Sol"}}],
        attachments=[],
        metadata={"to_me": True},
        has_reply_link=False,
        reply_to_bot_response=False,
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "ordinary_message"
    assert decision.features["bridge_to_me"] is True
    assert decision.features["to_me"] is False
    assert decision.features["summons_talk_bot"] is False


def test_chinese_lily_text_summons_talk_bot() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        observation_bot_id="985393579",
        text="莉莉 Sol",
        segments=[{"type": "text", "data": {"text": "莉莉 Sol"}}],
        attachments=[],
        metadata={},
        has_reply_link=False,
        reply_to_bot_response=False,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "summons_talk_bot"
    assert decision.features["to_me"] is True


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
