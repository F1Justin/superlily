import re
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]


def load_module(name: str, path: Path):
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_bridge_payloads():
    return (
        load_module(
            "superlily_lily_source_id_payloads",
            ROOT / "bridges" / "lily_nonebot" / "lily_core_bridge" / "payloads.py",
        ),
        load_module(
            "superlily_nekro_source_id_payloads",
            ROOT / "bridges" / "nekro" / "superlily_bridge" / "payloads.py",
        ),
    )


def test_message_source_event_id_v2_is_identical_across_bridges_and_content_free() -> None:
    lily, nekro = load_bridge_payloads()
    conversation = {"id": "708309706", "type": "group", "name": "private room name"}
    native_identity = {
        "message_id": "short-42",
        "real_seq": 998877,
        "time": 1750000000,
        "raw_message": "must not affect or appear in the id",
        "remote_url": "https://example.test/private?token=secret",
        "access_token": "top-secret-token",
    }

    lily_id = lily.message_source_event_id(
        conversation,
        "short-42",
        native_identity,
        sender_id="456",
        occurred_at="2026-07-15T10:00:00+00:00",
    )
    nekro_id = nekro.message_source_event_id(
        conversation,
        "short-42",
        native_identity,
        sender_id="456",
        occurred_at="2026-07-15T10:00:00+00:00",
    )

    assert lily_id == nekro_id
    assert re.fullmatch(r"qq:source:v2:[0-9a-f]{64}", lily_id)
    assert "private room name" not in lily_id
    assert "example.test" not in lily_id
    assert "secret" not in lily_id
    assert "must not affect" not in lily_id


def test_message_source_event_id_v2_distinguishes_reused_short_ids() -> None:
    for payloads in load_bridge_payloads():
        conversation = {"id": "708309706", "type": "group"}
        base = payloads.message_source_event_id(
            conversation,
            "short-42",
            {"message_id": "short-42", "real_seq": 100, "time": 1750000000},
        )
        changed_real_seq = payloads.message_source_event_id(
            conversation,
            "short-42",
            {"message_id": "short-42", "real_seq": 101, "time": 1750000000},
        )
        changed_native_time = payloads.message_source_event_id(
            conversation,
            "short-42",
            {"message_id": "short-42", "real_seq": 100, "time": 1750000001},
        )

        assert len({base, changed_real_seq, changed_native_time}) == 3
        assert base == payloads.message_source_event_id(
            conversation,
            "short-42",
            {"time": 1750000000, "real_seq": 100, "message_id": "short-42"},
        )


def test_message_source_event_id_v2_weak_identity_fallback_is_content_independent() -> None:
    for payloads in load_bridge_payloads():
        conversation = {"id": "708309706", "type": "group"}

        def source_id(
            short_id: str,
            sender_id: str,
            occurred_at: str,
            native_identity=None,
        ) -> str:
            return payloads.message_source_event_id(
                conversation,
                short_id,
                native_identity,
                sender_id=sender_id,
                occurred_at=occurred_at,
            )

        baseline = source_id("short-42", "456", "2026-07-15T10:00:00+00:00")
        replay = source_id(
            "short-42",
            "456",
            "2026-07-15T10:00:00+00:00",
            {
                "raw_message": "other text",
                "url": "https://example.test/should-not-be-identity",
                "secret": "should-not-be-identity",
            },
        )

        assert baseline == replay
        assert baseline != source_id("short-43", "456", "2026-07-15T10:00:00+00:00")
        assert baseline != source_id("short-42", "457", "2026-07-15T10:00:00+00:00")
        assert baseline != source_id("short-42", "456", "2026-07-15T10:00:01+00:00")


def test_lily_source_event_id_uses_v2_message_identity() -> None:
    lily, _ = load_bridge_payloads()
    conversation = {"id": "708309706", "type": "group"}
    raw = {
        "post_type": "message",
        "message_id": "short-42",
        "real_seq": 998877,
        "time": 1750000000,
        "group_id": 708309706,
        "user_id": 456,
    }
    event = SimpleNamespace(
        message_id="short-42",
        real_seq=998877,
        time=1750000000,
        user_id=456,
    )

    assert lily.source_event_id(event, conversation, raw) == lily.message_source_event_id(
        conversation,
        "short-42",
        lily.native_message_identity(raw, event),
        sender_id=456,
        occurred_at=1750000000,
    )


def test_lily_payload_uses_original_message_for_reply_segments() -> None:
    payloads = load_module(
        "superlily_lily_payloads",
        ROOT / "bridges" / "lily_nonebot" / "lily_core_bridge" / "payloads.py",
    )
    original_message = [
        {"type": "reply", "data": {"id": "1432599397"}},
        {"type": "at", "data": {"qq": "3643287298"}},
        {"type": "text", "data": {"text": " 测试引用1带at"}},
    ]
    event = SimpleNamespace(
        original_message=original_message,
        get_message=lambda: [{"type": "text", "data": {"text": "测试引用1带at"}}],
    )

    text, segments, _ = payloads.message_segments(payloads.event_message(event))
    references = payloads.message_references(segments, {"id": "708309706", "type": "group"})

    assert text == " 测试引用1带at"
    assert [segment["type"] for segment in segments] == ["reply", "at", "text"]
    assert references == [
        {
            "type": "reply_to",
            "platform_message_id": "1432599397",
            "conversation_id": "708309706",
            "conversation_type": "group",
            "raw": {"segment": {"type": "reply", "data": {"id": "1432599397"}}},
        }
    ]


def test_lily_native_identity_is_a_content_free_allowlist() -> None:
    payloads = load_module(
        "superlily_lily_identity_payloads",
        ROOT / "bridges" / "lily_nonebot" / "lily_core_bridge" / "payloads.py",
    )

    identity = payloads.native_message_identity(
        {
            "message_id": -101,
            "message_seq": -101,
            "real_id": -101,
            "real_seq": 998877,
            "time": 1750000000,
            "group_id": 123,
            "user_id": 456,
            "message_type": "group",
            "sub_type": "normal",
            "msgUid": "native-uid",
            "raw_message": "must not be copied",
            "access_token": "must not be copied",
            "sender": {"nickname": "must not be copied"},
        }
    )

    assert identity == {
        "schema": "onebot_v11.qq.native_identity.v1",
        "message_id": "-101",
        "message_seq": "-101",
        "real_id": "-101",
        "real_seq": "998877",
        "time": "1750000000",
        "group_id": "123",
        "user_id": "456",
        "message_type": "group",
        "sub_type": "normal",
        "msg_uid": "native-uid",
    }


def test_nekro_payload_uses_ext_data_ref_msg_id_for_reply_reference() -> None:
    payloads = load_module(
        "superlily_nekro_payloads",
        ROOT / "bridges" / "nekro" / "superlily_bridge" / "payloads.py",
    )
    conv = {"id": "708309706", "type": "group", "name": None}

    references = payloads.message_references(
        [{"type": "text", "text": "测试带at"}],
        conv,
        payloads.ref_msg_id_from_ext_data({"ref_msg_id": "419057009"}),
    )

    assert references == [
        {
            "type": "reply_to",
            "platform_message_id": "419057009",
            "conversation_id": "708309706",
            "conversation_type": "group",
            "raw": {"ext_data": {"ref_msg_id": "419057009"}},
        }
    ]


def test_nekro_native_identity_ignores_arbitrary_extension_data() -> None:
    payloads = load_module(
        "superlily_nekro_identity_payloads",
        ROOT / "bridges" / "nekro" / "superlily_bridge" / "payloads.py",
    )

    identity = payloads.native_message_identity(
        {
            "message_id": "nekro-local",
            "real_seq": "445566",
            "peerUid": "group-peer",
            "ref_msg_id": "not-an-identity-field",
            "cookie": "must not be copied",
            "remote_url": "https://example.test/private?token=1",
        }
    )

    assert identity == {
        "schema": "onebot_v11.qq.native_identity.v1",
        "message_id": "nekro-local",
        "real_seq": "445566",
        "peer_uid": "group-peer",
    }


def test_nekro_payload_deduplicates_segment_and_ext_reply_reference() -> None:
    payloads = load_module(
        "superlily_nekro_payloads",
        ROOT / "bridges" / "nekro" / "superlily_bridge" / "payloads.py",
    )

    references = payloads.message_references(
        [{"type": "reply", "data": {"id": "419057009"}}],
        {"id": "708309706", "type": "group", "name": None},
        "419057009",
    )

    assert len(references) == 1
    assert references[0]["platform_message_id"] == "419057009"
