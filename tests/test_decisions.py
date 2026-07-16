import re

import pytest

from superlily_core.command_registry import (
    CommandRegistry,
    CommandRule,
    load_command_registry,
    match_runtime_candidate,
    match_runtime_candidates,
    runtime_candidate_trigger_reviewed,
    runtime_match_supports_command,
)
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
        text="wf 1+1",
        attachments=[],
        metadata={"to_me": True},
        has_reply_link=False,
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
                kind="token",
                triggers=("status",),
                target_instance_id="lily-command",
                source_plugin="nonebot_plugin_picstatus",
            ),
            CommandRule(
                id="test.keyword",
                kind="contains",
                triggers=("magic",),
                target_instance_id="lily-command",
                source_plugin="plugins.demo",
            ),
            CommandRule(
                id="test.suffix",
                kind="suffix",
                triggers=("结束",),
                target_instance_id="lily-command",
                source_plugin="plugins.demo",
            ),
        ),
    )

    assert registry.match("fortune").rule_id == "test.fortune"
    assert registry.match("fortune cookie") is None
    assert registry.match("设置换老婆次数 3").permission == "group_admin"
    assert registry.match("status now").rule_id == "test.status"
    assert registry.match("statusquo") is None
    assert registry.match("prefix magic suffix").rule_id == "test.keyword"
    assert registry.match("现在结束").rule_id == "test.suffix"


def test_default_registry_covers_known_external_commands() -> None:
    registry = load_command_registry()

    train = registry.match("查询列车 G1")
    waifu = registry.match("今日老婆")
    sensitive = registry.match("重启nb")
    compact_wolfram = registry.match("wf1+1")
    false_train_prefix = registry.match("trainwreck")
    period_wordcloud = registry.match("我的本周词云")
    event_help = registry.match("eventhelp")
    random_selector = registry.match("随机莉莉白语录 任意后缀")
    random_mutation = registry.match("添加随机学养 新图片")

    assert train is not None
    assert train.source_plugin == "nonebot_plugin_cnrail"
    assert waifu is not None
    assert waifu.source_plugin == "nonebot_plugin_today_waifu"
    assert sensitive is not None
    assert sensitive.permission == "superuser"
    assert sensitive.sensitive is True
    assert compact_wolfram is not None
    assert compact_wolfram.rule_id == "lily.wolfram"
    assert false_train_prefix is None
    assert period_wordcloud is not None
    assert period_wordcloud.rule_id == "external.wordcloud.period"
    assert event_help is not None
    assert event_help.rule_id == "external.eventmonitor.help"
    assert random_selector is not None
    assert random_selector.rule_id == "external.random.draw"
    assert random_selector.trigger == "随机莉莉白语录"
    assert random_mutation is not None
    assert random_mutation.rule_id == "external.random.modify"
    assert random_mutation.permission == "group_admin"
    assert random_mutation.sensitive is True


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


def test_runtime_candidates_follow_reported_matcher_semantics() -> None:
    candidates = [
        {"plugin_id": "demo", "module_name": "demo", "kind": "token", "triggers": ["train"]},
        {"plugin_id": "demo", "module_name": "demo", "kind": "regex", "triggers": ["demo\\d+"]},
    ]
    assert match_runtime_candidate(candidates, "train G1")["trigger"] == "train"
    assert match_runtime_candidate(candidates, "trainwreck") is None
    assert match_runtime_candidate(candidates, "prefix demo42 suffix")["kind"] == "regex"


def test_runtime_command_candidates_use_nonebot_longest_prefix() -> None:
    candidates = [
        {"plugin_id": "random", "module_name": "random", "kind": "command", "triggers": ["随机莉莉"]},
        {
            "plugin_id": "random",
            "module_name": "random",
            "kind": "command",
            "triggers": ["随机莉莉白语录"],
        },
    ]

    assert match_runtime_candidates(candidates, "随机莉莉白语录 任意后缀") == [
        {
            "plugin_id": "random",
            "module_name": "random",
            "kind": "command",
            "trigger": "随机莉莉白语录",
            "complete": False,
            "rule_checker_count": None,
            "unknown_rule_checkers": [],
            "permission_checker_count": None,
            "ignore_case": None,
            "regex_flags": None,
        }
    ]


def test_static_command_registry_uses_nonebot_longest_prefix_across_rules() -> None:
    registry = CommandRegistry(
        version="test",
        rules=(
            CommandRule(
                id="short",
                kind="command",
                triggers=("随机莉莉",),
                target_instance_id="lily-command",
                source_plugin="nonebot-plugin-random",
            ),
            CommandRule(
                id="long",
                kind="command",
                triggers=("随机莉莉白语录",),
                target_instance_id="lily-command",
                source_plugin="nonebot-plugin-random",
            ),
        ),
    )

    match = registry.match("随机莉莉白语录 任意后缀")

    assert match is not None
    assert match.rule_id == "long"
    assert match.trigger == "随机莉莉白语录"


def test_runtime_command_coverage_is_bound_to_the_reviewed_plugin() -> None:
    registry = CommandRegistry(
        version="test",
        rules=(
            CommandRule(
                id="test.wolfram",
                kind="command",
                triggers=("wf",),
                target_instance_id="lily-command",
                source_plugin="plugins.wolfram",
            ),
        ),
    )
    candidates = [
        {"plugin_id": "collision", "module_name": "plugins.collision", "kind": "command", "triggers": ["wf"]},
        {"plugin_id": "wolfram", "module_name": "plugins.wolfram", "kind": "command", "triggers": ["wf"]},
    ]

    static_match = registry.match("wf 1+1")
    runtime_matches = match_runtime_candidates(candidates, "wf 1+1")

    assert static_match is not None
    assert len(runtime_matches) == 2
    assert runtime_match_supports_command(runtime_matches[0], static_match) is False
    assert runtime_match_supports_command(runtime_matches[1], static_match) is True


def test_static_registry_case_and_regex_flags_are_part_of_match_semantics() -> None:
    registry = CommandRegistry(
        version="strict-semantics",
        rules=(
            CommandRule(
                id="case-sensitive",
                kind="exact",
                triggers=("PING",),
                target_instance_id="lily-command",
                source_plugin="plugins.case_sensitive",
            ),
            CommandRule(
                id="case-insensitive",
                kind="exact",
                triggers=("HELLO",),
                target_instance_id="lily-command",
                source_plugin="plugins.case_insensitive",
                ignore_case=True,
            ),
            CommandRule(
                id="regex-insensitive",
                kind="regex",
                triggers=(r"^abc$",),
                target_instance_id="lily-command",
                source_plugin="plugins.regex",
                regex_flags=re.IGNORECASE,
            ),
        ),
    )

    assert registry.match("PING").rule_id == "case-sensitive"
    assert registry.match("ping") is None
    assert registry.match("hello").rule_id == "case-insensitive"
    assert registry.match("ABC").rule_id == "regex-insensitive"


def test_runtime_review_requires_exact_case_and_regex_flag_semantics() -> None:
    case_registry = CommandRegistry(
        version="case",
        rules=(
            CommandRule(
                id="case",
                kind="exact",
                triggers=("PING",),
                target_instance_id="lily-command",
                source_plugin="plugins.demo",
                ignore_case=False,
            ),
        ),
    )
    case_match = case_registry.match("PING")
    assert case_match is not None
    runtime_case_mismatch = {
        "plugin_id": "demo",
        "module_name": "plugins.demo",
        "kind": "exact",
        "trigger": "PING",
        "ignore_case": True,
        "regex_flags": None,
    }
    assert runtime_match_supports_command(runtime_case_mismatch, case_match) is False
    assert runtime_candidate_trigger_reviewed(
        case_registry,
        {**runtime_case_mismatch, "triggers": ["PING"]},
        "PING",
    ) is False

    regex_registry = CommandRegistry(
        version="regex",
        rules=(
            CommandRule(
                id="regex",
                kind="regex",
                triggers=(r"^abc$",),
                target_instance_id="lily-command",
                source_plugin="plugins.demo",
                regex_flags=re.IGNORECASE,
            ),
        ),
    )
    regex_match = regex_registry.match("ABC")
    assert regex_match is not None
    runtime_regex = {
        "plugin_id": "demo",
        "module_name": "plugins.demo",
        "kind": "regex",
        "trigger": r"^abc$",
        "triggers": [r"^abc$"],
        "ignore_case": False,
        "regex_flags": 0,
    }
    assert runtime_match_supports_command(runtime_regex, regex_match) is False
    assert runtime_candidate_trigger_reviewed(regex_registry, runtime_regex, r"^abc$") is False

    runtime_regex["regex_flags"] = int(re.IGNORECASE)
    assert runtime_match_supports_command(runtime_regex, regex_match) is True
    assert runtime_candidate_trigger_reviewed(regex_registry, runtime_regex, r"^abc$") is True


def test_decision_routes_explicit_bot_mention_to_talk() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="看看这个",
        attachments=[],
        metadata={},
        has_reply_link=False,
        mentioned_bot_instance_ids=["lily-command"],
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "explicit_bot_mention"
    assert decision.features["mentions_observing_bot"] is True


def test_bridge_to_me_does_not_override_core_summon_policy() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="Lily Sol",
        attachments=[],
        metadata={"to_me": True},
        has_reply_link=False,
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "ordinary_message"
    assert decision.features["bridge_to_me"] is True
    assert decision.features["to_me"] is False
    assert decision.features["summons_talk_bot"] is False


def test_known_bot_message_is_observed_without_retriggering_policy() -> None:
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
        text="莉莉，wf 1+1",
        attachments=[],
        metadata={"to_me": True},
        has_reply_link=False,
        command_registry=registry,
        sender_bot_instance_id="nekro-agent",
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "bot_message_observed"
    assert decision.features["sender_bot_instance_id"] == "nekro-agent"
    assert decision.features["to_me"] is False
    assert decision.features["summons_talk_bot"] is False


def test_chinese_lily_text_summons_talk_bot() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="莉莉 Sol",
        attachments=[],
        metadata={},
        has_reply_link=False,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "summons_talk_bot"
    assert decision.features["to_me"] is True


def test_decision_routes_reply_to_nekro_response_to_talk() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="继续",
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="nekro-agent",
        reply_target_status="resolved_bot",
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "reply_to_talk_response"


def test_reply_to_nekro_takes_precedence_over_lily_command() -> None:
    registry = load_command_registry()
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="换老婆",
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="nekro-agent",
        reply_target_status="resolved_bot",
        command_registry=registry,
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "reply_to_talk_response"
    assert decision.features["matched_command"]["rule_id"] == "external.today_waifu.public"


def test_decision_observes_reply_to_lily_response() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="继续",
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="lily-command",
        reply_target_status="resolved_bot",
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "reply_to_command_response_observed"


@pytest.mark.parametrize("text", ["换老婆", "莉莉继续"])
def test_reply_to_lily_takes_precedence_over_command_or_summon(text: str) -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="group",
        text=text,
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="lily-command",
        reply_target_status="resolved_bot",
        mentioned_bot_instance_ids=["nekro-agent"],
        command_registry=load_command_registry(),
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "reply_to_command_response_observed"


def test_command_only_group_allows_commands_but_not_conversation() -> None:
    registry = load_command_registry()
    command = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="随机学养评价什么都可以",
        attachments=[],
        metadata={},
        has_reply_link=False,
        command_registry=registry,
        conversation_mode="command_only",
    )
    summon = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="莉莉在吗",
        attachments=[],
        metadata={},
        has_reply_link=False,
        conversation_mode="command_only",
    )
    reply = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="继续",
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="nekro-agent",
        reply_target_status="resolved_bot",
        conversation_mode="command_only",
    )

    assert command.decision_type == "command"
    assert command.target_instance_id == "lily-command"
    assert command.features["matched_command"]["trigger"] == "随机学养"
    assert summon.reason == "conversation_mode_command_only"
    assert reply.reason == "conversation_mode_command_only"
    assert summon.decision_type == reply.decision_type == "observe_only"


def test_conversation_only_group_routes_talk_but_not_commands() -> None:
    command = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="换老婆",
        attachments=[],
        metadata={},
        has_reply_link=False,
        command_registry=load_command_registry(),
        conversation_mode="conversation_only",
    )
    summon = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="莉莉在吗",
        attachments=[],
        metadata={},
        has_reply_link=False,
        conversation_mode="conversation_only",
    )
    reply = decide_event(
        source_event_type="message",
        conversation_type="group",
        text="换老婆",
        attachments=[],
        metadata={},
        has_reply_link=True,
        reply_target_instance_id="nekro-agent",
        reply_target_status="resolved_bot",
        command_registry=load_command_registry(),
        conversation_mode="conversation_only",
    )

    assert command.decision_type == "observe_only"
    assert command.reason == "command_target_unavailable"
    assert summon.decision_type == reply.decision_type == "talk"
    assert summon.target_instance_id == reply.target_instance_id == "nekro-agent"
    assert reply.reason == "reply_to_talk_response"


def test_observe_only_group_never_routes_commands_or_conversation() -> None:
    decisions = [
        decide_event(
            source_event_type="message",
            conversation_type="group",
            text=text,
            attachments=[],
            metadata={},
            has_reply_link=reply,
            reply_target_instance_id="nekro-agent" if reply else None,
            reply_target_status="resolved_bot" if reply else "none",
            command_registry=load_command_registry(),
            conversation_mode="observe_only",
        )
        for text, reply in (("换老婆", False), ("莉莉在吗", False), ("换老婆", True))
    ]

    assert all(item.decision_type == "observe_only" for item in decisions)
    assert all(item.target_instance_id is None for item in decisions)
    assert [item.reason for item in decisions] == [
        "command_target_unavailable",
        "conversation_mode_observe_only",
        "conversation_mode_observe_only",
    ]


def test_private_lily_ordinary_message_is_observed() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="private",
        text="你好",
        attachments=[],
        metadata={},
        has_reply_link=False,
        observing_instance_id="lily-command",
    )

    assert decision.decision_type == "observe_only"
    assert decision.target_instance_id is None
    assert decision.reason == "private_recipient_observed"


def test_private_lily_command_is_routed_to_lily() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="private",
        text="wf 1+1",
        attachments=[],
        metadata={},
        has_reply_link=False,
        command_registry=load_command_registry(),
        observing_instance_id="lily-command",
    )

    assert decision.decision_type == "command"
    assert decision.target_instance_id == "lily-command"
    assert decision.reason == "command_prefix:wf"


def test_private_nekro_message_is_routed_to_nekro_without_summon() -> None:
    decision = decide_event(
        source_event_type="message",
        conversation_type="private",
        text="任意普通对话",
        attachments=[],
        metadata={},
        has_reply_link=False,
        command_registry=load_command_registry(),
        observing_instance_id="nekro-agent",
    )

    assert decision.decision_type == "talk"
    assert decision.target_instance_id == "nekro-agent"
    assert decision.reason == "private_recipient_talk"
