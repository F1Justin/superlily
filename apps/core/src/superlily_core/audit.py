from __future__ import annotations

from collections.abc import Iterable
from collections import Counter


def classify_decision_outcome(
    *,
    decision_type: str,
    target_instance_id: str | None,
    successful_instances: Iterable[str],
    failed_instances: Iterable[str],
    ambiguous_instances: Iterable[str] = (),
    age_seconds: int,
    grace_seconds: int,
) -> str:
    successful_counts = Counter(successful_instances)
    successful = set(successful_counts)
    failed = set(failed_instances)
    ambiguous = set(ambiguous_instances)
    expected = target_instance_id if decision_type in {"command", "talk"} else None
    if expected is None:
        return "unexpected_response" if successful or failed or ambiguous else "matched_no_response"
    if expected in successful:
        if successful - {expected}:
            return "matched_with_extra"
        if successful_counts[expected] > 1:
            return "duplicate_successful_target_response"
        return "matched"
    if expected in ambiguous:
        return "ambiguous_completion"
    if expected in failed:
        return "failed"
    if successful or ambiguous:
        return "wrong_instance"
    if age_seconds < grace_seconds:
        return "pending"
    return "missed"
