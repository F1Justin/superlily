from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import ModelProviderProfile, model_profile_hash
from superlily_core.agent_run_service import import_model_profile
from superlily_core.models import (
    AgentRun,
    AgentRunAttempt,
    AgentRunEvent,
    AgentToolProposalRecord,
    RenderDeliveryIntent,
    ToolInvocation,
)


def event_payload(
    *,
    source_event_id: str = "qq:group:phase5:message:1",
    message_id: str = "1",
    text: str = "帮我看一下莉莉现在的状态",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "instance": {
            "instance_id": "nekro-agent",
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": "2022692714",
            "role": "talk",
        },
        "event_type": "message",
        "conversation": {"id": "phase5", "type": "group", "name": "Phase 5 Test"},
        "sender": {"id": "123456", "name": "Tester", "roles": ["member"]},
        "message": {
            "id": message_id,
            "text": text,
            "segments": [{"type": "text", "data": {"text": text}}],
            "attachments": [],
        },
        "references": [],
        "occurred_at": now,
        "metadata": {},
    }


def profile() -> ModelProviderProfile:
    return ModelProviderProfile(
        provider_id="provider-model-shadow",
        version="1.0.0",
        title="Planner shadow test model",
        data_locality="regional",
        retention_seconds=0,
        structured_output_protocol="json_schema",
        context_window_tokens=16_384,
        max_output_tokens=2_048,
        permitted_data_classifications=["conversation"],
        pricing={
            "currency": "USD",
            "input_cache_hit_microunits_per_million_tokens": 1_000_000,
            "input_cache_miss_microunits_per_million_tokens": 1_000_000,
            "output_microunits_per_million_tokens": 1_000_000,
        },
        health_protocol="superlily-model-provider-v1",
    )


def run_payload(
    profile_hash: str,
    *,
    source_event_id: str = "qq:group:phase5:message:1",
) -> dict:
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "model_provider_id": "provider-model-shadow",
        "model_profile_version": "1.0.0",
        "model_profile_hash": profile_hash,
        "budget": {
            "max_model_attempts": 2,
            "max_model_turns": 1,
            "max_tool_proposals": 3,
            "max_tool_calls": 0,
            "max_sequential_depth": 0,
            "max_parallel_fanout": 0,
            "max_wall_time_ms": 30_000,
            "max_input_tokens": 4_096,
            "max_output_tokens": 1_024,
            "max_total_tokens": 5_120,
            "max_cost_microunits": 100_000,
            "max_input_bytes": 65_536,
            "max_output_bytes": 65_536,
            "max_result_bytes": 0,
            "max_artifact_bytes": 0,
        },
    }


def admin_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer admin-secret",
        "Idempotency-Key": key,
    }


def model_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer model-shadow-secret",
        "Idempotency-Key": key,
    }


async def prepare_shadow(client, app) -> tuple[str, str]:
    app.state.settings = replace(
        app.state.settings,
        agent_mode="shadow",
        model_provider_tokens={"provider-model-shadow": "model-shadow-secret"},
    )
    created_event = await client.post(
        "/v1/events",
        json=event_payload(),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "phase5-event-1",
        },
    )
    assert created_event.status_code == 201, created_event.text
    active_profile = profile()
    profile_hash = model_profile_hash(active_profile)
    async with app.state.database.sessions() as session:
        _, duplicate = await import_model_profile(
            session,
            active_profile,
            source_commit="1" * 40,
            bundle_hash=profile_hash,
            reviewer="phase5-test-reviewer",
        )
    assert duplicate is False
    return profile_hash, created_event.json()["source_event_id"]


def successful_attempt(*, tool_count: int = 1) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    tools = [
        {
            "tool_id": "status.inspect",
            "descriptor_version": "1.0.2",
            "descriptor_hash": "a" * 64,
            "arguments": {"scope": "provider_runtime"},
            "explanation": "The user asked for status.",
        }
        for _ in range(tool_count)
    ]
    return {
        "schema_version": "1.0",
        "outcome": "succeeded",
        "model_request_id": "model-request-1",
        "raw_output_sha256": "b" * 64,
        "usage": {
            "input_tokens": 10,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cost_microunits": 15,
            "input_bytes": 200,
            "output_bytes": 120,
            "wall_time_ms": 100,
        },
        "proposal": {
            "answer_markdown": None,
            "tool_proposals": tools,
            "uncertainty_basis_points": 500,
            "safe_summary": "Proposed a status lookup without executing it.",
        },
        "safe_error_code": None,
        "started_at": now,
        "completed_at": now,
    }


async def test_agent_mode_off_creates_no_run(client, app) -> None:
    event = await client.post(
        "/v1/events",
        json=event_payload(),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "phase5-off-event",
        },
    )
    assert event.status_code == 201
    response = await client.post(
        "/v1/agent-runs",
        json=run_payload("a" * 64),
        headers=admin_headers("phase5-off-run"),
    )
    assert response.status_code == 409
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(AgentRun.id))) == 0


async def test_shadow_records_forbidden_proposal_without_execution_or_delivery(
    client,
    app,
) -> None:
    profile_hash, source_event_id = await prepare_shadow(client, app)
    created = await client.post(
        "/v1/agent-runs",
        json=run_payload(profile_hash, source_event_id=source_event_id),
        headers=admin_headers("phase5-shadow-run-1"),
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["state"] == "context_ready"
    assert body["eligible_tool_count"] == 0
    assert body["tool_invocation_count"] == 0
    assert body["delivery_intent_count"] == 0
    run_id = body["run_id"]

    unauthorized = await client.get(
        f"/v1/agent-runs/{run_id}/planner-input",
        headers={"Authorization": "Bearer provider-status-secret"},
    )
    assert unauthorized.status_code == 401
    planner_input = await client.get(
        f"/v1/agent-runs/{run_id}/planner-input",
        headers={"Authorization": "Bearer model-shadow-secret"},
    )
    assert planner_input.status_code == 200, planner_input.text
    planner_body = planner_input.json()
    assert planner_body["tool_execution_authority"] is False
    assert planner_body["delivery_authority"] is False
    assert planner_body["context"]["current_message"]["text"] == "帮我看一下莉莉现在的状态"
    assert "input_schema" not in str(planner_body["context"]["eligible_tools"])

    attempt_payload = successful_attempt()
    reported = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        json=attempt_payload,
        headers=model_headers("phase5-shadow-attempt-1"),
    )
    assert reported.status_code == 201, reported.text
    result = reported.json()
    assert result["state"] == "shadow_complete"
    assert result["reason_code"] == "proposal_recorded_no_execution"
    assert result["proposal_validation_counts"] == {"forbidden_tool": 1}
    assert result["tool_invocation_count"] == 0
    assert result["delivery_intent_count"] == 0

    replay = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        json=attempt_payload,
        headers=model_headers("phase5-shadow-attempt-1"),
    )
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["attempt_id"] == result["attempt_id"]

    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(AgentRun.id))) == 1
        assert await session.scalar(select(func.count(AgentRunAttempt.id))) == 1
        assert await session.scalar(select(func.count(AgentToolProposalRecord.id))) == 1
        assert await session.scalar(select(func.count(ToolInvocation.id))) == 0
        assert await session.scalar(select(func.count(RenderDeliveryIntent.id))) == 0
        assert await session.scalar(select(func.count(AgentRunEvent.id))) == 3


async def test_failed_model_attempt_can_retry_but_never_execute(client, app) -> None:
    profile_hash, source_event_id = await prepare_shadow(client, app)
    created = await client.post(
        "/v1/agent-runs",
        json=run_payload(profile_hash, source_event_id=source_event_id),
        headers=admin_headers("phase5-shadow-retry-run"),
    )
    run_id = created.json()["run_id"]
    now = datetime.now(timezone.utc).isoformat()
    invalid = {
        "schema_version": "1.0",
        "outcome": "invalid_output",
        "model_request_id": "invalid-request",
        "raw_output_sha256": "c" * 64,
        "usage": {
            "input_tokens": 4,
            "input_cache_hit_tokens": 0,
            "input_cache_miss_tokens": 4,
            "output_tokens": 1,
            "total_tokens": 5,
            "cost_microunits": 5,
            "input_bytes": 100,
            "output_bytes": 10,
            "wall_time_ms": 50,
        },
        "proposal": None,
        "safe_error_code": "schema_invalid",
        "started_at": now,
        "completed_at": now,
    }
    first = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        json=invalid,
        headers=model_headers("phase5-invalid-attempt"),
    )
    assert first.status_code == 201, first.text
    assert first.json()["state"] == "context_ready"
    assert first.json()["attempt_count"] == 1

    second_payload = successful_attempt()
    second_payload["model_request_id"] = "model-request-2"
    second = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        json=second_payload,
        headers=model_headers("phase5-success-attempt"),
    )
    assert second.status_code == 201, second.text
    assert second.json()["state"] == "shadow_complete"
    assert second.json()["attempt_count"] == 2
    assert second.json()["tool_invocation_count"] == 0


async def test_shadow_budget_exhaustion_is_terminal_without_authority(
    client,
    app,
) -> None:
    profile_hash, source_event_id = await prepare_shadow(client, app)
    payload = run_payload(profile_hash, source_event_id=source_event_id)
    payload["budget"]["max_total_tokens"] = 10
    created = await client.post(
        "/v1/agent-runs",
        json=payload,
        headers=admin_headers("phase5-budget-run"),
    )
    assert created.status_code == 201, created.text
    reported = await client.post(
        f"/v1/agent-runs/{created.json()['run_id']}/attempts",
        json=successful_attempt(),
        headers=model_headers("phase5-budget-attempt"),
    )
    assert reported.status_code == 201, reported.text
    body = reported.json()
    assert body["state"] == "budget_exhausted"
    assert body["reason_code"] == "run_budget_exhausted"
    assert body["tool_invocation_count"] == 0
    assert body["delivery_intent_count"] == 0
    assert "max_total_tokens" in body["events"][-1]["evidence"]["budget_reasons"]


async def test_cancelled_model_attempt_records_core_interruption_without_authority(
    client,
    app,
) -> None:
    profile_hash, source_event_id = await prepare_shadow(client, app)
    created = await client.post(
        "/v1/agent-runs",
        json=run_payload(profile_hash, source_event_id=source_event_id),
        headers=admin_headers("phase5-cancelled-run"),
    )
    assert created.status_code == 201, created.text
    now = datetime.now(timezone.utc).isoformat()
    cancelled = await client.post(
        f"/v1/agent-runs/{created.json()['run_id']}/attempts",
        headers=model_headers("phase5-cancelled-attempt"),
        json={
            "schema_version": "1.0",
            "outcome": "cancelled",
            "model_request_id": None,
            "raw_output_sha256": "d" * 64,
            "usage": {
                "input_tokens": 0,
                "input_cache_hit_tokens": 0,
                "input_cache_miss_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_microunits": 0,
                "input_bytes": 0,
                "output_bytes": 0,
                "wall_time_ms": 0,
            },
            "proposal": None,
            "safe_error_code": "core_interrupted",
            "started_at": now,
            "completed_at": now,
        },
    )
    assert cancelled.status_code == 201, cancelled.text
    body = cancelled.json()
    assert body["state"] == "cancelled"
    assert body["reason_code"] == "core_interrupted"
    assert body["tool_invocation_count"] == 0
    assert body["delivery_intent_count"] == 0


async def test_agent_evidence_and_terminal_run_are_database_guarded(client, app) -> None:
    profile_hash, source_event_id = await prepare_shadow(client, app)
    created = await client.post(
        "/v1/agent-runs",
        json=run_payload(profile_hash, source_event_id=source_event_id),
        headers=admin_headers("phase5-guard-run"),
    )
    run_id = created.json()["run_id"]
    reported = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        json=successful_attempt(),
        headers=model_headers("phase5-guard-attempt"),
    )
    assert reported.status_code == 201

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run_id)
                .values(outcome="provider_error")
            )
            await session.commit()
        await session.rollback()

        with pytest.raises(DBAPIError):
            await session.execute(
                update(AgentRun)
                .where(AgentRun.id == run_id)
                .values(state="context_ready")
            )
            await session.commit()
        await session.rollback()
