from superlily_core.agent_shadow_scoring import (
    AgentShadowLabel,
    score_shadow_runs,
)


def run_view(
    run_id: str,
    *,
    proposals: list[dict],
    invalid: int = 0,
    forbidden: int = 0,
    duplicate: int = 0,
) -> dict:
    return {
        "run_id": run_id,
        "proposals": proposals,
        "proposal_validation_counts": {
            "valid": sum(item["validation"] == "valid" for item in proposals),
            "invalid_arguments": invalid,
            "forbidden_tool": forbidden,
            "duplicate_loop": duplicate,
        },
    }


def test_shadow_score_distinguishes_route_and_proposal_failures() -> None:
    score = score_shadow_runs(
        [
            (
                AgentShadowLabel("direct", None),
                run_view(
                    "direct",
                    proposals=[
                        {"tool_id": "wolfram.run", "validation": "valid"},
                    ],
                ),
            ),
            (
                AgentShadowLabel("missed", "wolfram.run"),
                run_view("missed", proposals=[]),
            ),
            (
                AgentShadowLabel("wrong", "wolfram.run"),
                run_view(
                    "wrong",
                    proposals=[
                        {"tool_id": "latex.render", "validation": "valid"},
                        {"tool_id": "history.search", "validation": "forbidden_tool"},
                    ],
                    invalid=1,
                    forbidden=1,
                    duplicate=1,
                ),
            ),
        ]
    )
    assert score.as_dict() == {
        "runs": 3,
        "false_calls": 1,
        "missed_calls": 1,
        "wrong_tools": 1,
        "invalid_arguments": 1,
        "forbidden_tool_requests": 1,
        "duplicate_loops": 1,
        "route_disagreements": 3,
    }
