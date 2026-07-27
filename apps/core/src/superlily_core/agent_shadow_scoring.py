"""Pure, offline Phase 5a shadow scoring.

Scoring consumes admin run views and human/deterministic labels.  It has no
database mutations, tool invocation client, or delivery client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentShadowLabel:
    run_id: str
    expected_tool_id: str | None


@dataclass(frozen=True, slots=True)
class AgentShadowScore:
    runs: int
    false_calls: int
    missed_calls: int
    wrong_tools: int
    invalid_arguments: int
    forbidden_tool_requests: int
    duplicate_loops: int
    route_disagreements: int

    def as_dict(self) -> dict[str, int]:
        return {
            "runs": self.runs,
            "false_calls": self.false_calls,
            "missed_calls": self.missed_calls,
            "wrong_tools": self.wrong_tools,
            "invalid_arguments": self.invalid_arguments,
            "forbidden_tool_requests": self.forbidden_tool_requests,
            "duplicate_loops": self.duplicate_loops,
            "route_disagreements": self.route_disagreements,
        }


def score_shadow_runs(
    labelled_runs: list[tuple[AgentShadowLabel, dict[str, Any]]],
) -> AgentShadowScore:
    false_calls = 0
    missed_calls = 0
    wrong_tools = 0
    invalid_arguments = 0
    forbidden_tool_requests = 0
    duplicate_loops = 0
    route_disagreements = 0

    for label, run in labelled_runs:
        if run.get("run_id") != label.run_id:
            raise ValueError("label run_id does not match the run view")
        proposals = run.get("proposals")
        if not isinstance(proposals, list):
            raise ValueError("run view must include proposal evidence")
        valid_tools = {
            item.get("tool_id")
            for item in proposals
            if isinstance(item, dict)
            and item.get("validation") == "valid"
            and isinstance(item.get("tool_id"), str)
        }
        counts = run.get("proposal_validation_counts")
        if not isinstance(counts, dict):
            raise ValueError("run view must include proposal validation counts")
        invalid_arguments += int(counts.get("invalid_arguments", 0))
        forbidden_tool_requests += int(counts.get("forbidden_tool", 0))
        duplicate_loops += int(counts.get("duplicate_loop", 0))

        expected = label.expected_tool_id
        actual_has_tool = bool(valid_tools)
        expected_has_tool = expected is not None
        if not expected_has_tool and actual_has_tool:
            false_calls += 1
        elif expected_has_tool and not actual_has_tool:
            missed_calls += 1
        elif expected is not None and expected not in valid_tools:
            wrong_tools += 1
        if (
            actual_has_tool != expected_has_tool
            or (expected is not None and expected not in valid_tools)
        ):
            route_disagreements += 1

    return AgentShadowScore(
        runs=len(labelled_runs),
        false_calls=false_calls,
        missed_calls=missed_calls,
        wrong_tools=wrong_tools,
        invalid_arguments=invalid_arguments,
        forbidden_tool_requests=forbidden_tool_requests,
        duplicate_loops=duplicate_loops,
        route_disagreements=route_disagreements,
    )
