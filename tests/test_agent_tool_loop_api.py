from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from superlily_contracts import (
    ModelProviderProfile,
    canonicalize_json_value,
    load_tool_descriptor,
    model_profile_hash,
)
from superlily_core.agent_run_service import import_model_profile
from superlily_core.models import (
    AgentToolProposalRecord,
    AgentRunAttempt,
    RenderDeliveryIntent,
    ToolInvocation,
)
from superlily_core.tool_registry_service import import_tool_descriptor


DESCRIPTOR_PATH = (
    Path(__file__).parents[1] / "registry/descriptors/wolfram.run/1.1.0.json"
)


def _event() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1.0",
        "source_event_id": "system:system:phase5-wolfram:message:1",
        "instance": {
            "instance_id": "nekro-agent",
            "platform": "system",
            "adapter": "phase5_probe",
            "bot_id": "superlily",
            "role": "talk",
        },
        "event_type": "message",
        "conversation": {
            "id": "phase5-wolfram",
            "type": "system",
            "name": "Phase 5 Wolfram Probe",
        },
        "sender": {"id": "phase5-reviewer", "name": "Reviewer", "roles": []},
        "message": {
            "id": "1",
            "text": "请计算 2+2",
            "segments": [{"type": "text", "data": {"text": "请计算 2+2"}}],
            "attachments": [],
        },
        "references": [],
        "occurred_at": now,
        "metadata": {},
    }


def _profile() -> ModelProviderProfile:
    return ModelProviderProfile(
        provider_id="deepseek-v4-pro",
        version="1.0.0",
        title="DeepSeek test planner",
        data_locality="regional",
        retention_seconds=None,
        structured_output_protocol="json_object",
        context_window_tokens=1_000_000,
        max_output_tokens=384_000,
        permitted_data_classifications=["conversation"],
        pricing={
            "currency": "USD",
            "input_cache_hit_microunits_per_million_tokens": 0,
            "input_cache_miss_microunits_per_million_tokens": 1_000_000,
            "output_microunits_per_million_tokens": 1_000_000,
        },
        health_protocol="superlily-model-provider-v1",
    )


async def test_bounded_wolfram_promotion_falls_back_without_exact_canary(
    client,
    app,
) -> None:
    app.state.settings = replace(
        app.state.settings,
        agent_mode="bounded_readonly",
        tool_execution_mode="ledger_only",
        model_provider_tokens={"deepseek-v4-pro": "deepseek-core-secret"},
    )
    event = await client.post(
        "/v1/events",
        json=_event(),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "phase5-wolfram-event",
        },
    )
    assert event.status_code == 201, event.text
    descriptor = load_tool_descriptor(DESCRIPTOR_PATH.read_bytes())
    profile = _profile()
    profile_hash = model_profile_hash(profile)
    async with app.state.database.sessions() as session:
        await import_model_profile(
            session,
            profile,
            source_commit="1" * 40,
            bundle_hash=profile_hash,
            reviewer="phase5-test",
        )
        await import_tool_descriptor(
            session,
            DESCRIPTOR_PATH.read_bytes(),
            source_commit="2" * 40,
            bundle_hash=descriptor.authority.sha256,
            reviewer="phase5-test",
        )

    run = await client.post(
        "/v1/agent-runs",
        headers={
            "Authorization": "Bearer admin-secret",
            "Idempotency-Key": "phase5-wolfram-run",
        },
        json={
            "schema_version": "1.0",
            "source_event_id": event.json()["source_event_id"],
            "model_provider_id": profile.provider_id,
            "model_profile_version": profile.version,
            "model_profile_hash": profile_hash,
            "budget": {
                "max_model_attempts": 1,
                "max_model_turns": 2,
                "max_tool_proposals": 1,
                "max_tool_calls": 1,
                "max_sequential_depth": 1,
                "max_parallel_fanout": 1,
                "max_wall_time_ms": 30000,
                "max_input_tokens": 4096,
                "max_output_tokens": 1024,
                "max_total_tokens": 5120,
                "max_cost_microunits": 100000,
                "max_input_bytes": 65536,
                "max_output_bytes": 65536,
                "max_result_bytes": 16384,
                "max_artifact_bytes": 0,
            },
        },
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["run_id"]
    now = datetime.now(timezone.utc).isoformat()
    attempt = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        headers={
            "Authorization": "Bearer deepseek-core-secret",
            "Idempotency-Key": "phase5-wolfram-model-attempt",
        },
        json={
            "schema_version": "1.0",
            "outcome": "succeeded",
            "model_request_id": "deepseek-test-1",
            "raw_output_sha256": "3" * 64,
            "usage": {
                "input_tokens": 10,
                "input_cache_hit_tokens": 0,
                "input_cache_miss_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_microunits": 15,
                "input_bytes": 100,
                "output_bytes": 100,
                "wall_time_ms": 100,
            },
            "proposal": {
                "answer_markdown": "A calculation proposal is captured separately.",
                "tool_proposals": [],
                "uncertainty_basis_points": 0,
                "safe_summary": "Captured a planner decision.",
            },
            "safe_error_code": None,
            "started_at": now,
            "completed_at": now,
        },
    )
    assert attempt.status_code == 201, attempt.text
    async with app.state.database.sessions() as session:
        proposal = await session.scalar(
            select(AgentToolProposalRecord).where(
                AgentToolProposalRecord.run_id == run_id
            )
        )
        assert proposal is None
        model_attempt = await session.scalar(
            select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id)
        )
        assert model_attempt is not None
        arguments = {"expression": "2+2"}
        arguments_hash = canonicalize_json_value(arguments).sha256
        proposal_hash = canonicalize_json_value(
            {
                "tool_id": "wolfram.run",
                "descriptor_version": "1.1.0",
                "descriptor_hash": descriptor.authority.sha256,
                "arguments_hash": arguments_hash,
            }
        ).sha256
        proposal = AgentToolProposalRecord(
            run_id=run_id,
            attempt_id=model_attempt.id,
            ordinal=0,
            tool_id="wolfram.run",
            descriptor_version="1.1.0",
            descriptor_hash=descriptor.authority.sha256,
            arguments_json=arguments,
            arguments_hash=arguments_hash,
            explanation="A bounded calculation is useful.",
            proposal_hash=proposal_hash,
            validation="valid",
            validation_reasons_json=[],
        )
        session.add(proposal)
        await session.commit()
        proposal_id = proposal.id

    promoted = await client.post(
        f"/v1/agent-runs/{run_id}/tool-loop",
        headers={"Authorization": "Bearer admin-secret"},
        json={"schema_version": "1.0", "proposal_id": proposal_id},
    )
    assert promoted.status_code == 201, promoted.text
    assert promoted.json()["state"] == "failed"
    assert promoted.json()["tool_invocation_count"] == 1
    assert promoted.json()["delivery_intent_count"] == 0
    async with app.state.database.sessions() as session:
        invocation = await session.scalar(
            select(ToolInvocation).where(ToolInvocation.creator_id == run_id)
        )
        assert invocation is not None
        assert invocation.creator_type == "agent"
        assert invocation.state == "recorded_only"
        assert (
            await session.scalar(select(RenderDeliveryIntent).limit(1))
        ) is None
