from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1] / "bridges" / "nekro" / "superlily_bridge" / "identity.py"
)


def load_identity_module():
    spec = spec_from_file_location("superlily_nekro_identity", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_onebot_chat_key_uses_bare_conversation_id() -> None:
    identity = load_identity_module()

    assert identity.conversation("onebot_v11-group_928225852") == {
        "id": "928225852",
        "type": "group",
        "name": None,
    }
    assert identity.conversation("onebot_v11-private_123") == {
        "id": "123",
        "type": "private",
        "name": None,
    }


def test_native_identity_cache_is_bounded_and_expires() -> None:
    identity = load_identity_module()
    cache = identity.NativeIdentityCache(max_entries=2, ttl_seconds=10)

    cache.put("first", {"message_id": "1"}, now=0)
    cache.put("second", {"message_id": "2"}, now=1)
    cache.put("third", {"message_id": "3"}, now=2)

    assert cache.pop("first", now=2) is None
    assert cache.pop("second", now=2) == {"message_id": "2"}
    assert cache.pop("third", now=13) is None


def test_native_identity_cache_key_includes_conversation() -> None:
    identity = load_identity_module()

    assert identity.native_identity_cache_key({"type": "group", "id": "123"}, "456") == "group:123:456"
