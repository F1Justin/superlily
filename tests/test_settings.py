import pytest

from superlily_core.settings import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stale_after_seconds", 0),
        ("correlation_window_seconds", -1),
        ("raw_max_bytes", 1_023),
        ("claim_mode", "unsafe"),
        ("tool_execution_mode", "unsafe"),
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
    with pytest.raises(ValueError, match="unique per provider"):
        Settings(provider_tokens={"provider-a": "same", "provider-b": "same"})
    with pytest.raises(ValueError, match="provider, admin, and ingest"):
        Settings(ingest_tokens={"lily-command": "same"}, provider_tokens={"provider-a": "same"})


def test_settings_reject_empty_environment_token(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_PROVIDER_TOKENS_JSON", '{"provider-a":""}')
    with pytest.raises(ValueError, match="non-empty string tokens"):
        Settings.from_env()


def test_provider_tokens_and_freshness_load_from_separate_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "SUPERLILY_PROVIDER_TOKENS_JSON",
        '{"provider-status-primary":"provider-only-secret"}',
    )
    monkeypatch.setenv("SUPERLILY_PROVIDER_INVENTORY_STALE_SECONDS", "321")
    monkeypatch.setenv("SUPERLILY_PROVIDER_HEARTBEAT_STALE_SECONDS", "45")

    settings = Settings.from_env()

    assert settings.provider_tokens == {"provider-status-primary": "provider-only-secret"}
    assert settings.provider_inventory_stale_seconds == 321
    assert settings.provider_heartbeat_stale_seconds == 45


def test_invocation_ledger_mode_loads_without_enabling_leases(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "ledger_only")
    monkeypatch.setenv("SUPERLILY_TOOL_GLOBAL_STOP", "true")

    settings = Settings.from_env()

    assert settings.tool_execution_mode == "ledger_only"
    assert settings.tool_lease_seconds == 15
    assert settings.tool_reaper_interval_seconds == 1
    assert settings.tool_global_stop is True


def test_executable_tool_mode_requires_exact_reviewed_scope(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "enforce")

    with pytest.raises(ValueError, match="explicit reviewed scope"):
        Settings.from_env()


def test_exact_canary_scope_loads_without_implicit_wildcards(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "canary")
    monkeypatch.setenv(
        "SUPERLILY_TOOL_CANARY_SCOPES_JSON",
        '[{"tool_id":"status.inspect","descriptor_version":"1.0.0",'
        '"descriptor_hash":"' + "a" * 64 + '",'
        '"canonical_conversation":"qq:group:1080353942","caller":"admin_api",'
        '"provider_id":"provider-status-primary"}]',
    )
    monkeypatch.setenv("SUPERLILY_TOOL_LEASE_SECONDS", "12")

    settings = Settings.from_env()

    assert settings.tool_execution_mode == "canary"
    assert settings.tool_lease_seconds == 12
    assert len(settings.tool_canary_scopes) == 1


def test_rollout_scope_rejects_noncanonical_conversation(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "canary")
    monkeypatch.setenv(
        "SUPERLILY_TOOL_CANARY_SCOPES_JSON",
        '[{"tool_id":"status.inspect","descriptor_version":"1.0.0",'
        '"descriptor_hash":"' + "a" * 64 + '",'
        '"canonical_conversation":"1080353942","caller":"admin_api",'
        '"provider_id":"provider-status-primary"}]',
    )
    with pytest.raises(ValueError, match="platform:type:id"):
        Settings.from_env()


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
