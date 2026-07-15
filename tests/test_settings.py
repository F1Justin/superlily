import pytest

from superlily_core.settings import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stale_after_seconds", 0),
        ("correlation_window_seconds", -1),
        ("raw_max_bytes", 1_023),
        ("claim_mode", "unsafe"),
    ],
)
def test_settings_reject_unsafe_control_plane_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_settings_reject_reused_authentication_tokens() -> None:
    with pytest.raises(ValueError, match="unique per instance"):
        Settings(ingest_tokens={"lily-command": "same", "nekro-agent": "same"})
    with pytest.raises(ValueError, match="admin and ingest"):
        Settings(admin_token="same", ingest_tokens={"lily-command": "same"})


def test_group_modes_are_explicit_and_private_messages_stay_full(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_GROUP_DEFAULT_MODE", "command_only")
    monkeypatch.setenv(
        "SUPERLILY_GROUP_MODES_JSON",
        '{"qq:group:123":"full","qq:group:456":"command_only",'
        '"qq:group:789":"conversation_only","qq:group:999":"observe_only"}',
    )
    settings = Settings.from_env()

    assert settings.conversation_mode("qq", "group", "123") == "full"
    assert settings.conversation_mode("qq", "group", "456") == "command_only"
    assert settings.conversation_mode("qq", "group", "789") == "conversation_only"
    assert settings.conversation_mode("qq", "group", "999") == "observe_only"
    assert settings.conversation_mode("qq", "group", "1000") == "command_only"
    assert settings.conversation_mode("qq", "private", "789") == "full"


def test_group_modes_reject_unknown_mode(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_GROUP_MODES_JSON", '{"qq:group:123":"chatty"}')
    with pytest.raises(ValueError, match="command_only, conversation_only, full, or observe_only"):
        Settings.from_env()


def test_group_modes_reject_noncanonical_key(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_GROUP_MODES_JSON", '{"123":"full"}')
    with pytest.raises(ValueError, match="platform:group:id"):
        Settings.from_env()
