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
        ("tool_confirmation_seconds", 29),
        ("control_preview_seconds", 14),
        ("control_mutation_attempts", 0),
        ("control_mutation_window_seconds", 59),
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
    with pytest.raises(ValueError, match="model provider tokens must be unique"):
        Settings(model_provider_tokens={"model-a": "same", "model-b": "same"})
    with pytest.raises(
        ValueError,
        match="model provider, tool provider, admin, and ingest",
    ):
        Settings(
            provider_tokens={"provider-a": "same"},
            model_provider_tokens={"model-a": "same"},
        )


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


def test_agent_shadow_requires_an_independent_model_provider_token(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_AGENT_MODE", "shadow")
    with pytest.raises(ValueError, match="requires at least one model provider token"):
        Settings.from_env()

    monkeypatch.setenv(
        "SUPERLILY_MODEL_PROVIDER_TOKENS_JSON",
        '{"provider-model-shadow":"model-only-secret"}',
    )
    settings = Settings.from_env()

    assert settings.agent_mode == "shadow"
    assert settings.model_provider_tokens == {
        "provider-model-shadow": "model-only-secret"
    }
    assert settings.agent_context_window_messages == 12


def test_invocation_ledger_mode_loads_without_enabling_leases(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "ledger_only")
    monkeypatch.setenv("SUPERLILY_TOOL_GLOBAL_STOP", "true")

    settings = Settings.from_env()

    assert settings.tool_execution_mode == "ledger_only"
    assert settings.tool_lease_seconds == 15
    assert settings.tool_confirmation_seconds == 120
    assert settings.tool_reaper_interval_seconds == 1
    assert settings.tool_global_stop is True


def test_artifact_store_requires_a_paired_narrow_root_and_private_pepper(tmp_path) -> None:
    assert Settings().artifact_enabled is False
    with pytest.raises(ValueError, match="configured together"):
        Settings(artifact_root=str(tmp_path / "artifacts"))
    with pytest.raises(ValueError, match="at least 32"):
        Settings(
            artifact_root=str(tmp_path / "artifacts"),
            artifact_secret_pepper="short",
        )
    with pytest.raises(ValueError, match="narrow absolute"):
        Settings(artifact_root="/", artifact_secret_pepper="p" * 32)
    settings = Settings(
        artifact_root=str(tmp_path / "artifacts"),
        artifact_secret_pepper="p" * 32,
    )
    assert settings.artifact_enabled is True
    assert settings.artifact_secret_pepper not in repr(settings)


def test_enforce_mode_remains_closed_until_reviewed_plan_support(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "enforce")

    with pytest.raises(ValueError, match="enforce is not open"):
        Settings.from_env()


def test_canary_ceiling_loads_without_environment_scope(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "canary")
    monkeypatch.setenv("SUPERLILY_TOOL_LEASE_SECONDS", "12")
    monkeypatch.setenv("SUPERLILY_TOOL_CONFIRMATION_SECONDS", "90")

    settings = Settings.from_env()

    assert settings.tool_execution_mode == "canary"
    assert settings.tool_lease_seconds == 12
    assert settings.tool_confirmation_seconds == 90


def test_environment_rollout_scope_cannot_create_authority(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "canary")
    monkeypatch.setenv(
        "SUPERLILY_TOOL_CANARY_SCOPES_JSON",
        '[{"tool_id":"status.inspect","descriptor_version":"1.0.0",'
        '"descriptor_hash":"' + "a" * 64 + '",'
        '"canonical_conversation":"qq:group:1080353942","caller":"admin_api",'
        '"provider_id":"provider-status-primary"}]',
    )
    with pytest.raises(ValueError, match="is obsolete"):
        Settings.from_env()


def test_obsolete_rollout_scope_rejects_even_noncanonical_legacy_content(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_TOOL_EXECUTION_MODE", "canary")
    monkeypatch.setenv(
        "SUPERLILY_TOOL_CANARY_SCOPES_JSON",
        '[{"tool_id":"status.inspect","descriptor_version":"1.0.0",'
        '"descriptor_hash":"' + "a" * 64 + '",'
        '"canonical_conversation":"1080353942","caller":"admin_api",'
        '"provider_id":"provider-status-primary"}]',
    )
    with pytest.raises(ValueError, match="is obsolete"):
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


def test_control_operators_require_complete_exact_boundary_configuration(monkeypatch) -> None:
    verifier = f"scrypt$16384$8$1${'A' * 22}${'B' * 43}"
    monkeypatch.setenv(
        "SUPERLILY_CONTROL_OPERATORS_JSON",
        '{"schema_version":"1.0","operators":['
        '{"operator_id":"reviewer.one","role":"reviewer",'
        f'"password_hash":"{verifier}","enabled":true}}]}}',
    )

    with pytest.raises(ValueError, match="allowed hosts, allowed origins"):
        Settings.from_env()

    monkeypatch.setenv("SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON", '["control.test"]')
    monkeypatch.setenv(
        "SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON",
        '["https://control.test"]',
    )
    monkeypatch.setenv(
        "SUPERLILY_CONTROL_AUDIT_PEPPER",
        "control-test-audit-pepper-32-bytes-minimum",
    )
    settings = Settings.from_env()

    assert settings.control_operators["reviewer.one"].role == "reviewer"
    assert settings.control_allowed_hosts == frozenset({"control.test"})
    assert settings.control_allowed_origins == frozenset({"https://control.test"})
    assert verifier not in repr(settings)
    assert settings.control_audit_pepper not in repr(settings)


def test_control_configuration_rejects_wildcard_or_non_https_authority(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON", '["*.example.test"]')

    with pytest.raises(ValueError, match="invalid exact Host"):
        Settings.from_env()

    monkeypatch.setenv("SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON", '["control.test"]')
    monkeypatch.setenv(
        "SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON",
        '["http://control.test"]',
    )

    with pytest.raises(ValueError, match="HTTPS origins"):
        Settings.from_env()
