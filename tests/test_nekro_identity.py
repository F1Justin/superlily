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


def test_outbound_suppression_requires_exact_conversation_and_source() -> None:
    identity = load_identity_module()
    tracker = identity.OutboundSuppressionTracker(ttl_seconds=60)
    conv = {"type": "group", "id": "708309706"}

    installed = tracker.install(
        conv,
        "event:denied",
        "claim:deny",
        "decision_target:lily-command",
        now=0,
    )

    assert installed.acknowledged is False
    assert tracker.match(conv, "event:denied", now=1) == installed
    assert tracker.match(conv, "event:other", now=1) is None
    assert tracker.match({"type": "group", "id": "other"}, "event:denied", now=1) is None


def test_outbound_suppression_ack_state_is_persisted_for_audit() -> None:
    identity = load_identity_module()
    tracker = identity.OutboundSuppressionTracker()
    conv = {"type": "private", "id": "42"}
    tracker.install(conv, "event:one", "claim:one", "peer_target", now=0)

    updated = tracker.set_acknowledged(conv, "event:one", True, now=1)
    retry_after_timeout = tracker.set_acknowledged(conv, "event:one", False, now=2)

    assert updated is not None and updated.acknowledged is True
    assert retry_after_timeout is not None and retry_after_timeout.acknowledged is True
    assert tracker.match(conv, "event:one", now=3).acknowledged is True


def test_outbound_suppression_is_bounded_and_expires() -> None:
    identity = load_identity_module()
    tracker = identity.OutboundSuppressionTracker(max_entries=2, ttl_seconds=10)
    conv = {"type": "group", "id": "7"}

    tracker.install(conv, "event:first", "claim:first", "deny", now=0)
    tracker.install(conv, "event:second", "claim:second", "deny", now=1)
    tracker.install(conv, "event:third", "claim:third", "deny", now=2)

    assert len(tracker) == 2
    assert tracker.match(conv, "event:first", now=2) is None
    assert tracker.match(conv, "event:second", now=12) is None
    assert tracker.match(conv, "event:third", now=13) is None


def test_response_trigger_tracker_preserves_1207_task_attribution() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker(ttl_seconds=180)
    conv = {"type": "group", "id": "1080353942"}

    tracker.remember(conv, "event:first", None, now=0)
    # The second message arrives while task 101 is still running.  It must not
    # overwrite the source even when task 101 has not emitted anything yet.
    tracker.remember(conv, "event:second", 101, now=1)
    assert tracker.source_for_response(conv, 101, now=2) == "event:first"
    assert tracker.source_for_response(conv, 101, now=3) == "event:first"
    assert tracker.source_for_response(conv, 202, now=4) == "event:second"


def test_response_trigger_tracker_reuses_source_for_multiple_task_outputs() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker()
    conv = {"type": "private", "id": "42"}

    tracker.remember(conv, "event:one", None, now=0)

    assert tracker.source_for_response(conv, "task-one", now=1) == "event:one"
    assert tracker.source_for_response(conv, "task-one", now=2) == "event:one"
    assert tracker.source_for_response(conv, "task-one", now=3) == "event:one"


def test_repeated_source_binds_when_task_appears_without_creating_pending() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker()
    conv = {"type": "group", "id": "43"}

    tracker.remember(conv, "event:one", None, now=0)
    tracker.remember(conv, "event:one", "task-one", now=1)

    assert tracker.source_for_response(conv, "task-one", now=2) == "event:one"
    assert tracker.source_for_response(conv, "task-two", now=3) is None


def test_response_trigger_tracker_mirrors_debounce_and_pending_replacement() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker()
    conv = {"type": "group", "id": "7"}

    tracker.remember(conv, "event:debounced-old", None, now=0)
    tracker.remember(conv, "event:debounced-new", None, now=1)
    assert tracker.source_for_response(conv, "active", now=2) == "event:debounced-new"

    tracker.remember(conv, "event:pending-old", "active", now=3)
    tracker.remember(conv, "event:pending-new", "active", now=4)
    tracker.remember(conv, "event:pending-new", "active", now=5)
    assert tracker.source_for_response(conv, "active", now=6) == "event:debounced-new"
    assert tracker.source_for_response(conv, "next", now=7) == "event:pending-new"


def test_source_less_system_task_does_not_steal_pending_trigger() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker()
    conv = {"type": "group", "id": "8"}

    assert tracker.source_for_response(conv, "system-task", now=0) is None
    tracker.remember(conv, "event:user", "system-task", now=1)

    # Further output from the system task remains unlinked.  Its token must
    # change before the queued user trigger becomes current.
    assert tracker.source_for_response(conv, "system-task", now=2) is None
    assert tracker.source_for_response(conv, "user-task", now=3) == "event:user"


def test_response_trigger_tracker_forget_removes_only_requested_source() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker()
    conv = {"type": "group", "id": "9"}

    tracker.remember(conv, "event:current", None, now=0)
    assert tracker.source_for_response(conv, "task-current", now=1) == "event:current"
    tracker.remember(conv, "event:denied", "task-current", now=2)
    tracker.forget(conv, "event:denied", now=3)

    assert tracker.source_for_response(conv, "task-current", now=4) == "event:current"
    assert tracker.source_for_response(conv, "task-next", now=5) is None


def test_response_trigger_tracker_is_bounded_and_expires() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker(max_entries=2, ttl_seconds=10)
    first = {"type": "group", "id": "1"}
    second = {"type": "group", "id": "2"}
    third = {"type": "group", "id": "3"}

    tracker.remember(first, "event:first", None, now=0)
    tracker.remember(second, "event:second", None, now=1)
    tracker.remember(third, "event:third", None, now=2)

    assert len(tracker) == 2
    assert tracker.source_for_response(first, "task", now=2) is None
    assert tracker.source_for_response(second, "task", now=12) is None
    assert tracker.source_for_response(third, "task", now=13) is None


def test_response_trigger_tracker_preserves_active_task_past_ttl() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker(ttl_seconds=10)
    conv = {"type": "group", "id": "long-running"}

    tracker.remember(conv, "event:first", None, now=0)
    assert tracker.source_for_response(conv, "task-one", now=1) == "event:first"
    tracker.remember(conv, "event:second", "task-one", now=2)

    # A slow model/tool task remains authoritative even after the ordinary
    # idle-state TTL. The pending message moves only when the scheduler token
    # changes.
    assert tracker.source_for_response(conv, "task-one", now=600) == "event:first"
    assert tracker.source_for_response(conv, "task-two", now=601) == "event:second"


def test_response_trigger_tracker_does_not_preserve_expired_stale_task() -> None:
    identity = load_identity_module()
    tracker = identity.ResponseTriggerTracker(ttl_seconds=10)
    conv = {"type": "group", "id": "stale-task"}

    tracker.remember(conv, "event:first", None, now=0)
    assert tracker.source_for_response(conv, "task-one", now=1) == "event:first"

    # A different task token after the TTL cannot inherit an expired source.
    assert tracker.source_for_response(conv, "task-two", now=600) is None


def test_claim_targets_requested_instance() -> None:
    identity = load_identity_module()

    assert identity.claim_targets_instance(
        {
            "ready": True,
            "action": "allow",
            "reason": "decision_target:nekro-agent",
        },
        "nekro-agent",
    )


def test_claim_target_rejects_non_allowing_or_malformed_results() -> None:
    identity = load_identity_module()

    assert not identity.claim_targets_instance(
        {"ready": True, "action": "allow", "reason": "decision_target:lily-command"},
        "nekro-agent",
    )
    assert not identity.claim_targets_instance(
        {"ready": False, "action": "allow", "reason": "decision_target:nekro-agent"},
        "nekro-agent",
    )
    assert not identity.claim_targets_instance(
        {"ready": True, "action": "deny", "reason": "decision_target:nekro-agent"},
        "nekro-agent",
    )
    assert not identity.claim_targets_instance(None, "nekro-agent")
    assert not identity.claim_targets_instance("allow", "nekro-agent")


def test_claim_decision_target_survives_safe_coordination_abstain() -> None:
    identity = load_identity_module()
    claim = {
        "ready": False,
        "action": "abstain",
        "reason": "claim_peers_not_denied",
        "features": {
            "gates": {
                "decision_type": "talk",
                "target_instance_id": "nekro-agent",
            }
        },
    }

    assert identity.claim_decision_targets_instance(claim, "nekro-agent")
    assert not identity.claim_decision_targets_instance(claim, "lily-command")
    assert not identity.claim_decision_targets_instance(
        {**claim, "action": "deny"},
        "nekro-agent",
    )
