from superlily_core.claims import enforcement_enabled, evaluate_claim


def _features(**overrides):
    value = {
        "command_registry_error": None,
        "command_registry_runtime": {
            "status": "fresh",
            "runtime_match": {"trigger": "wf", "complete": True},
            "unregistered_match": None,
        },
        "matched_command": {"permission": "public", "sensitive": False},
        "reply_target_status": "none",
    }
    value.update(overrides)
    return value


def test_claim_routes_only_actionable_deterministic_decisions() -> None:
    allowed = evaluate_claim(
        mode="canary",
        requesting_instance_id="lily-command",
        decision_type="command",
        target_instance_id="lily-command",
        confidence=95,
        decision_features=_features(),
        correlation_version="qq-message-v3",
        observation_count=2,
        required_observations=2,
        minimum_confidence=85,
        target_status="online",
    )
    denied = evaluate_claim(
        mode="canary",
        requesting_instance_id="nekro-agent",
        decision_type="command",
        target_instance_id="lily-command",
        confidence=95,
        decision_features=_features(),
        correlation_version="qq-message-v3",
        observation_count=2,
        required_observations=2,
        minimum_confidence=85,
        target_status="online",
    )
    observed = evaluate_claim(
        mode="canary",
        requesting_instance_id="lily-command",
        decision_type="observe_only",
        target_instance_id=None,
        confidence=100,
        decision_features=_features(),
        correlation_version="qq-message-v3",
        observation_count=2,
        required_observations=2,
        minimum_confidence=85,
        target_status=None,
    )

    assert (allowed.action, allowed.ready) == ("allow", True)
    assert (denied.action, denied.ready) == ("deny", True)
    assert allowed.gates["decision_type"] == "command"
    assert allowed.gates["target_instance_id"] == "lily-command"
    assert (observed.action, observed.reason, observed.ready) == (
        "abstain",
        "non_actionable_decision",
        False,
    )


def test_claim_abstains_on_every_fail_open_gate() -> None:
    base = {
        "mode": "canary",
        "requesting_instance_id": "nekro-agent",
        "decision_type": "talk",
        "target_instance_id": "nekro-agent",
        "confidence": 95,
        "decision_features": _features(),
        "correlation_version": "qq-message-v3",
        "observation_count": 2,
        "required_observations": 2,
        "minimum_confidence": 85,
        "target_status": "online",
    }
    cases = [
        ({"correlation_version": None}, "strong_correlation_required"),
        ({"observation_count": 1}, "insufficient_observations"),
        ({"confidence": 80}, "confidence_below_threshold"),
        ({"decision_features": _features(command_registry_error="broken")}, "command_registry_unavailable"),
        (
            {"decision_features": _features(command_registry_runtime={"status": "stale"})},
            "runtime_registry_not_fresh",
        ),
        (
            {
                "decision_features": _features(
                    command_registry_runtime={
                        "status": "fresh",
                        "runtime_match": {"trigger": "new"},
                        "unregistered_match": {"trigger": "new"},
                    }
                )
            },
            "unregistered_runtime_command",
        ),
        (
            {
                "decision_type": "command",
                "target_instance_id": "lily-command",
                "decision_features": _features(
                    command_registry_runtime={
                        "status": "fresh",
                        "runtime_match": None,
                        "unregistered_match": None,
                    }
                ),
            },
            "command_not_confirmed_at_runtime",
        ),
        (
            {
                "decision_type": "command",
                "target_instance_id": "lily-command",
                "decision_features": _features(
                    command_registry_runtime={
                        "status": "fresh",
                        "runtime_match": {"trigger": "wf", "complete": False},
                        "unregistered_match": None,
                    }
                ),
            },
            "runtime_match_not_fully_introspected",
        ),
        (
            {
                "decision_type": "command",
                "target_instance_id": "lily-command",
                "decision_features": _features(
                    matched_command={"permission": "superuser", "sensitive": True}
                ),
            },
            "sensitive_command_not_enforced",
        ),
        (
            {
                "decision_type": "command",
                "target_instance_id": "lily-command",
                "decision_features": _features(
                    matched_command={"permission": "group_admin", "sensitive": False}
                ),
            },
            "command_permission_not_modeled",
        ),
        ({"decision_features": _features(reply_target_status="unresolved")}, "reply_target_not_deterministic"),
        ({"target_status": "offline"}, "target_instance_not_online"),
    ]
    for changes, reason in cases:
        result = evaluate_claim(**{**base, **changes})
        assert (result.action, result.reason, result.ready) == ("abstain", reason, False)


def test_claim_enforcement_scope_is_explicit() -> None:
    canaries = frozenset({"qq:group:123"})
    assert enforcement_enabled(
        mode="canary",
        platform="qq",
        conversation_type="group",
        conversation_id="123",
        canary_conversations=canaries,
    )
    assert not enforcement_enabled(
        mode="canary",
        platform="qq",
        conversation_type="group",
        conversation_id="456",
        canary_conversations=canaries,
    )
    assert enforcement_enabled(
        mode="enforce",
        platform="qq",
        conversation_type="group",
        conversation_id="456",
        canary_conversations=frozenset(),
    )
