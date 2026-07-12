from __future__ import annotations

from collections.abc import Iterable


def classify_decision_outcome(
    *,
    decision_type: str,
    target_instance_id: str | None,
    successful_instances: Iterable[str],
    failed_instances: Iterable[str],
    age_seconds: int,
    grace_seconds: int,
) -> str:
    successful = set(successful_instances)
    failed = set(failed_instances)
    expected = target_instance_id if decision_type in {"command", "talk"} else None
    if expected is None:
        return "unexpected_response" if successful or failed else "matched_no_response"
    if expected in successful:
        return "matched_with_extra" if successful - {expected} else "matched"
    if expected in failed:
        return "failed"
    if successful:
        return "wrong_instance"
    if age_seconds < grace_seconds:
        return "pending"
    return "missed"
