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
