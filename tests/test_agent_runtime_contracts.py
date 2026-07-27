from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from superlily_contracts import (
    AgentAttemptReportIn,
    AgentBudget,
    AgentContextMessage,
    AgentContextSnapshot,
    AgentPrincipalSnapshot,
    AgentProposal,
    AgentRunCreateIn,
    AgentToolProposal,
    AgentUsage,
    ModelPricing,
    ModelProviderProfile,
    agent_context_hash,
    agent_run_request_hash,
    model_profile_hash,
)


def model_profile() -> ModelProviderProfile:
    return ModelProviderProfile(
        provider_id="provider-model-shadow",
        version="1.0.0",
        title="Planner shadow model",
        data_locality="regional",
        retention_seconds=0,
        structured_output_protocol="json_schema",
        context_window_tokens=16_384,
        max_output_tokens=2_048,
        permitted_data_classifications=["public", "conversation"],
        pricing=ModelPricing(
            currency="USD",
            input_microunits_per_million_tokens=1_000_000,
            output_microunits_per_million_tokens=2_000_000,
        ),
        health_protocol="superlily-model-provider-v1",
    )


def budget() -> AgentBudget:
    return AgentBudget(
        max_model_attempts=2,
        max_model_turns=1,
        max_tool_proposals=3,
        max_wall_time_ms=30_000,
        max_input_tokens=4_096,
        max_output_tokens=1_024,
        max_total_tokens=5_120,
        max_cost_microunits=100_000,
        max_input_bytes=65_536,
        max_output_bytes=65_536,
    )


def test_model_profile_and_run_request_have_stable_content_identity() -> None:
    profile = model_profile()
    first = model_profile_hash(profile)
    second = model_profile_hash(ModelProviderProfile.model_validate(profile.model_dump()))
    assert first == second
    assert len(first) == 64

    payload = AgentRunCreateIn(
        source_event_id="qq:group:1:message:2",
        model_provider_id=profile.provider_id,
        model_profile_version=profile.version,
        model_profile_hash=first,
        budget=budget(),
    )
    request_hash = agent_run_request_hash(
        payload,
        creator_type="admin_api",
        creator_id="core-admin",
    )
    assert len(request_hash) == 64
    assert request_hash != agent_run_request_hash(
        payload,
        creator_type="admin_api",
        creator_id="another-admin",
    )


def test_phase5a_budget_has_zero_execution_and_delivery_dimensions() -> None:
    active = budget()
    assert active.max_tool_calls == 0
    assert active.max_sequential_depth == 0
    assert active.max_parallel_fanout == 0
    assert active.max_result_bytes == 0
    assert active.max_artifact_bytes == 0

    with pytest.raises(ValidationError):
        AgentBudget.model_validate(
            {
                **active.model_dump(),
                "max_tool_calls": 1,
            }
        )


def test_context_binds_current_event_and_rejects_duplicate_tools() -> None:
    now = datetime.now(timezone.utc)
    principal = AgentPrincipalSnapshot(
        platform="qq",
        sender_id="123",
        conversation_key="qq:group:456",
        conversation_type="group",
        source_event_id="event-current",
    )
    current = AgentContextMessage(
        source_event_id="event-current",
        sender_id="123",
        text="帮我看看状态",
        occurred_at=now,
        relation="current",
    )
    context = AgentContextSnapshot(
        policy_version="policy-v1",
        prompt_version="prompt-v1",
        system_policy="Model output is a request, not authority.",
        principal=principal,
        current_message=current,
        reply_graph=[],
        recent_messages=[],
        capabilities=[],
        eligible_tools=[],
        data_classification="conversation",
        retention_seconds=0,
    )
    assert len(agent_context_hash(context)) == 64

    with pytest.raises(ValidationError):
        AgentContextSnapshot.model_validate(
            {
                **context.model_dump(mode="json"),
                "current_message": {
                    **current.model_dump(mode="json"),
                    "source_event_id": "different-event",
                },
            }
        )


def test_attempt_contract_separates_success_from_safe_failure() -> None:
    now = datetime.now(timezone.utc)
    proposal = AgentProposal(
        answer_markdown=None,
        tool_proposals=[
            AgentToolProposal(
                tool_id="status.inspect",
                descriptor_version="1.0.2",
                descriptor_hash="a" * 64,
                arguments={"scope": "provider_runtime"},
                explanation="The user requested current status.",
            )
        ],
        uncertainty_basis_points=500,
        safe_summary="Proposed one read-only status call.",
    )
    usage = AgentUsage(
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_microunits=20,
        input_bytes=100,
        output_bytes=80,
        wall_time_ms=120,
    )
    succeeded = AgentAttemptReportIn(
        outcome="succeeded",
        model_request_id="request-1",
        raw_output_sha256="b" * 64,
        usage=usage,
        proposal=proposal,
        started_at=now,
        completed_at=now,
    )
    assert succeeded.safe_error_code is None

    with pytest.raises(ValidationError):
        AgentAttemptReportIn.model_validate(
            {
                **succeeded.model_dump(mode="json"),
                "outcome": "invalid_output",
                "safe_error_code": "schema_invalid",
            }
        )

    failed = AgentAttemptReportIn(
        outcome="invalid_output",
        model_request_id="request-2",
        raw_output_sha256="c" * 64,
        usage=usage,
        safe_error_code="schema_invalid",
        started_at=now,
        completed_at=now,
    )
    assert failed.proposal is None
