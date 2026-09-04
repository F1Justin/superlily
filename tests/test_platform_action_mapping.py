import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from superlily_contracts import EventIn
from superlily_core.models import (
    ConversationNameObservation,
    EventObservation,
    IdentityNameObservation,
    PlatformActionObservation,
)


ROOT = Path(__file__).parents[1]
ACTION_PATHS = [
    ROOT / "bridges/lily_nonebot/lily_core_bridge/platform_actions.py",
    ROOT / "bridges/nekro/superlily_bridge/platform_actions.py",
]
GROUP = {"id": "861651713", "type": "group", "name": None}


def load_module(path: Path):
    name = f"platform_actions_{path.parts[-3]}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance(instance_id: str = "lily-command", bot_id: str = "2022692714") -> dict:
    return {
        "instance_id": instance_id,
        "platform": "qq",
        "adapter": "onebot_v11",
        "bot_id": bot_id,
        "role": "command" if instance_id == "lily-command" else "agent",
        "display_name": instance_id,
        "version": "test",
    }


def build(module, raw: dict, *, instance_ref: dict | None = None) -> EventIn:
    post_type = raw.get("post_type")
    payload = module.platform_action_event_payload(
        raw,
        GROUP,
        instance_ref or instance(),
        event_type=(
            f"request.{raw.get('request_type')}"
            if post_type == "request"
            else (
                f"notice.notify.{raw.get('sub_type')}"
                if raw.get("notice_type") == "notify"
                else f"notice.{raw.get('notice_type')}"
            )
        ),
        fallback_occurred_at="2026-07-18T12:00:00+00:00",
    )
    assert payload is not None
    return EventIn.model_validate(payload)


def real_reaction(**changes) -> dict:
    raw = {
        "time": 1784200114,
        "self_id": 2022692714,
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "group_id": 861651713,
        "user_id": 1287260950,
        "message_id": 1822167647,
        "likes": [{"emoji_id": "212", "count": 1}],
    }
    raw.update(changes)
    return raw


def test_bridge_platform_action_implementations_are_identical() -> None:
    assert ACTION_PATHS[0].read_bytes() == ACTION_PATHS[1].read_bytes()


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_real_napcat_reaction_is_factual_observed_state(path: Path) -> None:
    event = build(load_module(path), real_reaction())

    assert event.capture is not None
    assert event.capture.status == "complete"
    assert event.capture.sanitizer_version == "onebot-v11-actions-v2"
    assert event.source_event_id.startswith("qq:action:v1:")
    assert len(event.actions) == 1
    action = event.actions[0]
    assert action.action_kind == "reaction"
    assert action.operation == "observed_state"
    assert action.actor_principal_id == "1287260950"
    assert action.target_platform_message_id == "1822167647"
    assert action.value == {"emoji_id": "212", "count": 1}
    assert "feedback" not in json.dumps(event.model_dump(mode="json"))


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_reaction_missing_actor_value_or_target_stays_explicit(path: Path) -> None:
    module = load_module(path)

    missing_actor = build(module, real_reaction(user_id=None))
    assert missing_actor.capture is not None
    assert missing_actor.capture.status == "partial"
    assert missing_actor.actions[0].actor_principal_id is None
    assert missing_actor.actions[0].capture_status == "partial"
    assert "actor" in str(missing_actor.actions[0].reason)

    missing_value = build(module, real_reaction(likes=[]))
    assert missing_value.capture is not None
    assert missing_value.capture.status == "partial"
    assert missing_value.actions[0].value == {}
    assert "likes value missing" in str(missing_value.actions[0].reason)

    missing_target = build(module, real_reaction(message_id=None))
    assert missing_target.capture is not None
    assert missing_target.capture.status == "unavailable"
    assert missing_target.actions == []
    assert "action omitted" in str(missing_target.capture.reason)


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_real_recall_separates_operator_subject_and_target(path: Path) -> None:
    module = load_module(path)
    event = build(
        module,
        {
            "time": 1784343432,
            "self_id": 2022692714,
            "post_type": "notice",
            "notice_type": "group_recall",
            "user_id": 3025419186,
            "group_id": 861651713,
            "operator_id": 445566,
            "message_id": 1426588331,
        },
    )
    action = event.actions[0]
    assert action.action_kind == "recall"
    assert action.operation == "remove"
    assert action.actor_principal_id == "445566"
    assert action.subject_principal_id == "3025419186"
    assert action.target_platform_message_id == "1426588331"

    missing_target = build(
        module,
        {
            "time": 1784343432,
            "post_type": "notice",
            "notice_type": "group_recall",
            "user_id": 3025419186,
            "group_id": 861651713,
            "operator_id": 445566,
        },
    )
    assert missing_target.actions[0].target_platform_message_id is None
    assert missing_target.actions[0].capture_status == "partial"
    assert "target message_id missing" in str(missing_target.actions[0].reason)


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_real_poke_keeps_display_fact_but_drops_urls_and_internal_uids(path: Path) -> None:
    module = load_module(path)
    raw = {
        "time": 1784343614,
        "self_id": 2022692714,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "user_id": 1083309783,
        "group_id": 861651713,
        "target_id": 1756110201,
        "raw_info": [
            {"type": "qq", "uid": "u_private_actor"},
            {
                "type": "img",
                "jp": "https://example.test/nudge?actionId=1&effectId=15&token=secret",
                "src": "http://example.test/private.jpg",
            },
            {"txt": "拍了拍", "type": "nor"},
            {"type": "qq", "uid": "u_private_target"},
            {"txt": "的米，说v你50", "type": "nor"},
        ],
    }
    event = build(module, raw)
    action = event.actions[0]
    assert action.action_kind == "poke"
    assert action.operation == "observed_state"
    assert action.actor_principal_id == "1083309783"
    assert action.subject_principal_id == "1756110201"
    assert action.value == {
        "sub_type": "poke",
        "display_text": "拍了拍的米，说v你50",
        "action_id": "1",
        "effect_id": "15",
    }
    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    assert "example.test" not in serialized
    assert "u_private" not in serialized
    assert "secret" not in serialized
    assert event.capture is not None
    assert "raw.raw_info[*].jp" in event.capture.omitted_fields
    assert "raw.raw_info[*].src" in event.capture.omitted_fields
    assert "raw.raw_info[*].uid" in event.capture.omitted_fields

    missing_target = build(module, {**raw, "target_id": None})
    assert missing_target.actions == []
    assert missing_target.capture is not None
    assert missing_target.capture.status == "unavailable"


def test_action_identity_is_cross_bridge_stable_and_observer_neutral() -> None:
    lily = load_module(ACTION_PATHS[0])
    nekro = load_module(ACTION_PATHS[1])
    raw = real_reaction()
    other_observer = {**raw, "self_id": 3348805846}

    first = lily.action_source_event_id(raw, GROUP)
    assert first == nekro.action_source_event_id(other_observer, GROUP)
    assert first != lily.action_source_event_id(
        real_reaction(likes=[{"emoji_id": "212", "count": 0}]),
        GROUP,
    )
    assert first != lily.action_source_event_id(real_reaction(user_id=99), GROUP)


HIGH_VALUE_ACTIONS = [
    (
        {
            "time": 1784344001,
            "post_type": "notice",
            "notice_type": "group_card",
            "group_id": 861651713,
            "user_id": 10001,
            "card_old": "Old Card",
            "card_new": "New Card",
        },
        "group_card",
        "update",
        "10001",
        {"card_old": "Old Card", "card_new": "New Card"},
    ),
    (
        {
            "time": 1784344002,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "group_name",
            "group_id": 861651713,
            "user_id": 10002,
            "name_new": "Renamed Group",
        },
        "group_name",
        "update",
        "qq:group:861651713",
        {"name_new": "Renamed Group"},
    ),
    (
        {
            "time": 1784344003,
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "group_id": 861651713,
            "operator_id": 10002,
            "user_id": 10003,
        },
        "group_membership",
        "add",
        "10003",
        {"sub_type": "approve"},
    ),
    (
        {
            "time": 1784344004,
            "post_type": "notice",
            "notice_type": "group_decrease",
            "sub_type": "kick",
            "group_id": 861651713,
            "operator_id": 10002,
            "user_id": 10003,
        },
        "group_membership",
        "remove",
        "10003",
        {"sub_type": "kick"},
    ),
    (
        {
            "time": 1784344005,
            "post_type": "notice",
            "notice_type": "group_admin",
            "sub_type": "set",
            "group_id": 861651713,
            "user_id": 10004,
        },
        "group_role",
        "update",
        "10004",
        {"role": "admin", "active": True, "sub_type": "set"},
    ),
    (
        {
            "time": 1784344006,
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "group_id": 861651713,
            "operator_id": 10002,
            "user_id": 10005,
            "duration": 600,
        },
        "group_ban",
        "update",
        "10005",
        {"sub_type": "ban", "duration_seconds": 600},
    ),
    (
        {
            "time": 1784344007,
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "title",
            "group_id": 861651713,
            "user_id": 10006,
            "title": "群之龙王",
        },
        "group_title",
        "update",
        "10006",
        {"title": "群之龙王"},
    ),
    (
        {
            "time": 1784344008,
            "post_type": "notice",
            "notice_type": "essence",
            "sub_type": "add",
            "group_id": 861651713,
            "operator_id": 10002,
            "sender_id": 10007,
            "message_id": 556677,
        },
        "essence",
        "add",
        "10007",
        {"sub_type": "add"},
    ),
    (
        {
            "time": 1784344009,
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 861651713,
            "user_id": 10008,
            "file": {
                "id": "file-1",
                "name": "notes.pdf",
                "size": 1234,
                "busid": 102,
                "download_url": "https://example.test/private",
            },
        },
        "group_file",
        "add",
        "10008",
        {"file_id": "file-1", "name": "notes.pdf", "size_bytes": 1234, "busid": "102"},
    ),
    (
        {
            "time": 1784344010,
            "post_type": "notice",
            "notice_type": "friend_add",
            "user_id": 10009,
        },
        "friendship",
        "add",
        "10009",
        {},
    ),
    (
        {
            "time": 1784344011,
            "post_type": "request",
            "request_type": "friend",
            "user_id": 10010,
            "comment": "你好",
            "flag": "opaque-friend-request",
        },
        "friend_request",
        "observed_state",
        "10010",
        {
            "request_type": "friend",
            "comment": "你好",
            "flag": "opaque-friend-request",
        },
    ),
    (
        {
            "time": 1784344012,
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 861651713,
            "user_id": 10011,
            "comment": "申请入群",
            "flag": "opaque-group-request",
        },
        "group_request",
        "observed_state",
        "10011",
        {
            "request_type": "group",
            "sub_type": "add",
            "comment": "申请入群",
            "flag": "opaque-group-request",
        },
    ),
    (
        {
            "time": 1784344013,
            "post_type": "notice",
            "notice_type": "bot_offline",
            "user_id": 2022692714,
            "tag": "network",
            "message": "connection lost",
        },
        "bot_status",
        "observed_state",
        "2022692714",
        {"online": False, "tag": "network", "message": "connection lost"},
    ),
]


@pytest.mark.parametrize("path", ACTION_PATHS)
@pytest.mark.parametrize(("raw", "kind", "operation", "subject", "value"), HIGH_VALUE_ACTIONS)
def test_high_value_onebot_facts_are_structured(
    path: Path,
    raw: dict,
    kind: str,
    operation: str,
    subject: str,
    value: dict,
) -> None:
    event = build(load_module(path), raw)

    assert event.capture is not None
    assert event.capture.status == "complete"
    assert len(event.actions) == 1
    action = event.actions[0]
    assert action.action_kind == kind
    assert action.operation == operation
    assert action.subject_principal_id == subject
    assert action.value == value
    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    assert "example.test/private" not in serialized
    if raw.get("notice_type") == "group_upload":
        assert "raw.file.download_url" in event.capture.omitted_fields


@pytest.mark.parametrize("path", ACTION_PATHS)
def test_incomplete_high_value_fact_is_explicit(path: Path) -> None:
    event = build(
        load_module(path),
        {
            "post_type": "notice",
            "notice_type": "group_ban",
            "group_id": 861651713,
            "user_id": 10005,
        },
    )

    assert event.capture is not None
    assert event.capture.status == "partial"
    assert event.actions[0].capture_status == "partial"
    assert "operator_id missing" in str(event.actions[0].reason)
    assert "platform event time missing" in str(event.actions[0].reason)


@pytest.mark.asyncio
async def test_group_card_and_group_name_actions_feed_name_history(client, app) -> None:
    module = load_module(ACTION_PATHS[0])
    raws = [HIGH_VALUE_ACTIONS[0][0], HIGH_VALUE_ACTIONS[1][0]]
    for index, raw in enumerate(raws, start=1):
        payload = module.platform_action_event_payload(
            raw,
            GROUP,
            instance(),
            event_type=f"name-action-{index}",
            fallback_occurred_at="2026-07-18T12:00:00+00:00",
        )
        assert payload is not None
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"name-action-{index}",
            },
        )
        assert response.status_code == 201, response.text

    async with app.state.database.sessions() as session:
        identity_names = (
            await session.scalars(select(IdentityNameObservation))
        ).all()
        conversation_names = (
            await session.scalars(select(ConversationNameObservation))
        ).all()

    assert [
        (row.user_id, row.name_kind, row.name_value, row.conversation_id)
        for row in identity_names
    ] == [("10001", "conversation_display_name", "New Card", "861651713")]
    assert [
        (row.conversation_id, row.name_value) for row in conversation_names
    ] == [("861651713", "Renamed Group")]


@pytest.mark.asyncio
async def test_both_bridge_payloads_persist_as_distinct_observations(client, app) -> None:
    raw = real_reaction()
    responses = []
    for path, instance_ref, token in (
        (ACTION_PATHS[0], instance("lily-command", "2022692714"), "lily-secret"),
        (ACTION_PATHS[1], instance("nekro-agent", "3348805846"), "nekro-secret"),
    ):
        module = load_module(path)
        payload = module.platform_action_event_payload(
            {**raw, "self_id": int(instance_ref["bot_id"])},
            GROUP,
            instance_ref,
            event_type="notice.group_msg_emoji_like",
            fallback_occurred_at="2026-07-18T12:00:00+00:00",
        )
        assert payload is not None
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"action-{instance_ref['instance_id']}",
            },
        )
        assert response.status_code == 201, response.text
        responses.append(response.json())

    async with app.state.database.sessions() as session:
        observations = [
            await session.get(EventObservation, response["observation_id"])
            for response in responses
        ]
        assert all(observation is not None for observation in observations)
        assert len(
            {observation.reported_source_event_id for observation in observations if observation}
        ) == 1
        actions = (
            await session.scalars(
                select(PlatformActionObservation).order_by(
                    PlatformActionObservation.observer_instance_id
                )
            )
        ).all()
        assert len(actions) == 2
        assert {action.actor_principal_id for action in actions} == {"1287260950"}
        assert {action.target_platform_message_id for action in actions} == {"1822167647"}
        assert len({action.observer_instance_id for action in actions}) == 2
