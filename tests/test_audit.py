from superlily_core.audit import classify_decision_outcome


def test_decision_outcome_classifier_covers_expected_and_unexpected_responses() -> None:
    assert classify_decision_outcome(
        decision_type="talk",
        target_instance_id="nekro-agent",
        successful_instances=["nekro-agent"],
        failed_instances=[],
        age_seconds=10,
        grace_seconds=30,
    ) == "matched"
    assert classify_decision_outcome(
        decision_type="command",
        target_instance_id="lily-command",
        successful_instances=["nekro-agent"],
        failed_instances=[],
        age_seconds=60,
        grace_seconds=30,
    ) == "wrong_instance"
    assert classify_decision_outcome(
        decision_type="observe_only",
        target_instance_id=None,
        successful_instances=["lily-command"],
        failed_instances=[],
        age_seconds=60,
        grace_seconds=30,
    ) == "unexpected_response"
    assert classify_decision_outcome(
        decision_type="talk",
        target_instance_id="nekro-agent",
        successful_instances=[],
        failed_instances=[],
        age_seconds=10,
        grace_seconds=30,
    ) == "pending"
    assert classify_decision_outcome(
        decision_type="talk",
        target_instance_id="nekro-agent",
        successful_instances=[],
        failed_instances=[],
        age_seconds=60,
        grace_seconds=30,
    ) == "missed"
    assert classify_decision_outcome(
        decision_type="talk",
        target_instance_id="nekro-agent",
        successful_instances=["nekro-agent", "nekro-agent"],
        failed_instances=[],
        age_seconds=10,
        grace_seconds=30,
    ) == "duplicate_successful_target_response"
