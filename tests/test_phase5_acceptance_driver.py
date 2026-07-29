from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx

from superlily_contracts import (
    AgentAttemptReportIn,
    ModelProviderProfile,
    model_profile_hash,
)
from superlily_core.agent_run_service import import_model_profile
from superlily_core.phase5_acceptance_driver import (
    AcceptanceConfig,
    CANONICAL_CONVERSATION,
    DESCRIPTOR_HASH,
    PROFILE_HASH,
    Phase5AcceptanceDriver,
    _budget,
    _event,
)
from superlily_model_provider.deepseek import PlannerAttempt


PROFILE_PATH = (
    Path(__file__).parents[1]
    / "registry/model_providers/deepseek-v4-pro/1.0.0.json"
)


def _config() -> AcceptanceConfig:
    return AcceptanceConfig(
        scenario="shadow",
        core_url="http://test",
        admin_token="admin-secret",
        ingest_token="nekro-secret",
        model_token="model-phase5-secret",
        deepseek_api_key="deepseek-test-secret",
        ingest_instance_id="nekro-agent",
        run_id="shadow-driver-1",
        source_commit="1" * 40,
        wait_seconds=30,
    )


def test_acceptance_payload_is_fixed_to_system_and_zero_authority() -> None:
    config = _config()
    payload = _event(config, bounded=False)
    assert payload["instance"]["platform"] == "system"
    assert payload["conversation"]["type"] == "system"
    assert payload["conversation"]["id"] == "phase5-acceptance"
    assert payload["metadata"]["no_platform_delivery"] is True
    assert _budget(bounded=False)["max_tool_calls"] == 0
    assert _budget(bounded=False)["max_result_bytes"] == 0
    bounded = _budget(bounded=True)
    assert bounded["max_model_attempts"] == 2
    assert bounded["max_model_turns"] == 2
    assert bounded["max_tool_calls"] == 1
    assert bounded["max_sequential_depth"] == 1
    assert bounded["max_parallel_fanout"] == 1
    assert bounded["max_artifact_bytes"] == 0


async def test_shadow_driver_runs_real_core_path_without_execution_or_delivery(
    app,
) -> None:
    app.state.settings = replace(
        app.state.settings,
        agent_mode="shadow",
        model_provider_tokens={"deepseek-v4-pro": "model-phase5-secret"},
    )
    profile = ModelProviderProfile.model_validate(
        json.loads(PROFILE_PATH.read_text())
    )
    assert model_profile_hash(profile) == PROFILE_HASH
    async with app.state.database.sessions() as session:
        _, duplicate = await import_model_profile(
            session,
            profile,
            source_commit="1" * 40,
            bundle_hash=PROFILE_HASH,
            reviewer="phase5-driver-test",
        )
    assert duplicate is False

    async def model_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/chat/completions"
        assert request.headers["authorization"] == "Bearer deepseek-test-secret"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-pro"
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(
            200,
            json={
                "id": "phase5-driver-model-request",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer_markdown": "4",
                                    "tool_proposals": [],
                                    "uncertainty_basis_points": 0,
                                    "safe_summary": "Answered directly.",
                                }
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            },
        )

    driver = Phase5AcceptanceDriver(
        _config(),
        core_transport=httpx.ASGITransport(app=app),
        model_transport=httpx.MockTransport(model_handler),
    )
    async with driver:
        evidence = await driver.run_shadow()
    assert evidence["conversation_key"] == CANONICAL_CONVERSATION
    assert evidence["state"] == "shadow_complete"
    assert evidence["attempts"][0]["outcome"] == "succeeded"
    assert evidence["tool_invocation_count"] == 0
    assert evidence["delivery_intent_count"] == 0


async def test_bounded_driver_verifies_authority_and_completes_without_delivery() -> None:
    config = replace(
        _config(),
        scenario="bounded-wolfram",
        run_id="bounded-driver-1",
        expected_plan_id="phase5-wolfram-agent-test",
        expected_plan_hash="a" * 64,
    )
    core_calls: dict[str, int] = {}

    async def core_handler(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.path}"
        core_calls[key] = core_calls.get(key, 0) + 1
        if key == "GET /v1/tools/wolfram.run":
            return httpx.Response(
                200,
                json={
                    "execution": {
                        "mode": "canary",
                        "leases_enabled": True,
                        "natural_language_callers": True,
                        "active_rollout_plan": {
                            "plan_id": config.expected_plan_id,
                            "plan_hash": config.expected_plan_hash,
                            "max_invocations": 1,
                            "consumed_invocations": 0,
                        },
                    },
                    "versions": [
                        {
                            "version": "1.1.0",
                            "desired": {
                                "descriptor_hash": DESCRIPTOR_HASH,
                                "lifecycle": "active",
                                "allowed_callers": [
                                    "command",
                                    "agent",
                                    "admin_api",
                                ],
                                "natural_language": True,
                            },
                            "effective": {"eligible": True},
                        }
                    ],
                },
            )
        if key == "POST /v1/events":
            body = json.loads(request.content)
            assert body["instance"]["platform"] == "system"
            assert body["conversation"]["type"] == "system"
            return httpx.Response(201, json={"source_event_id": "event:bounded-driver"})
        if key == "POST /v1/agent-runs":
            body = json.loads(request.content)
            assert body["budget"]["max_model_attempts"] == 2
            assert body["budget"]["max_tool_calls"] == 1
            return httpx.Response(
                201,
                json={
                    "run_id": "agent-run-bounded",
                    "conversation_key": CANONICAL_CONVERSATION,
                    "delivery_intent_count": 0,
                    "tool_invocation_count": 0,
                    "model_profile_hash": PROFILE_HASH,
                },
            )
        if key in {
            "GET /v1/agent-runs/agent-run-bounded/planner-input",
            "GET /v1/agent-tool-loops/agent-loop-bounded/planner-input",
        }:
            return httpx.Response(
                200,
                json={
                    "tool_execution_authority": False,
                    "delivery_authority": False,
                },
            )
        if key == "POST /v1/agent-runs/agent-run-bounded/attempts":
            return httpx.Response(
                201,
                json={
                    "proposals": [
                        {
                            "proposal_id": "proposal-bounded",
                            "tool_id": "wolfram.run",
                            "descriptor_version": "1.1.0",
                            "descriptor_hash": DESCRIPTOR_HASH,
                            "validation": "valid",
                        }
                    ]
                },
            )
        if key == "POST /v1/agent-runs/agent-run-bounded/tool-loop":
            return httpx.Response(
                201,
                json={
                    "loop_id": "agent-loop-bounded",
                    "invocation_id": "invocation-bounded",
                    "state": "tool_pending",
                    "tool_invocation_count": 1,
                    "delivery_intent_count": 0,
                },
            )
        if key == "GET /v1/agent-tool-loops/agent-loop-bounded":
            return httpx.Response(
                200,
                json={
                    "state": "result_ready",
                    "terminal_at": None,
                    "result_hash": "b" * 64,
                    "result_bytes": 256,
                },
            )
        if key == "POST /v1/agent-tool-loops/agent-loop-bounded/attempts":
            return httpx.Response(
                201,
                json={
                    "state": "complete",
                    "reason_code": "bounded_loop_complete_no_delivery",
                    "continuation_attempt_count": 1,
                    "tool_invocation_count": 1,
                    "delivery_intent_count": 0,
                },
            )
        if key == "GET /v1/tool-invocations/invocation-bounded":
            return httpx.Response(
                200,
                json={
                    "state": "succeeded",
                    "selected_provider_id": "provider-wolfram-primary",
                    "creator": {"type": "agent", "id": "agent-run-bounded"},
                },
            )
        if key == "GET /v1/agent-runs/agent-run-bounded":
            return httpx.Response(
                200,
                json={
                    "conversation_key": CANONICAL_CONVERSATION,
                    "state": "shadow_complete",
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "provider_id": "deepseek-v4-pro",
                            "outcome": "succeeded",
                            "model_request_id": "model-plan",
                            "safe_error_code": None,
                            "usage": {"total_tokens": 120},
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected Core request: {key}")

    class FakePlanner:
        def __init__(self) -> None:
            self.calls = 0

        async def plan(self, _: dict) -> PlannerAttempt:
            self.calls += 1
            now = datetime.now(timezone.utc)
            proposal = (
                {
                    "answer_markdown": None,
                    "tool_proposals": [
                        {
                            "tool_id": "wolfram.run",
                            "descriptor_version": "1.1.0",
                            "descriptor_hash": DESCRIPTOR_HASH,
                            "arguments": {"expression": "2+2"},
                            "explanation": "Use the exact bounded calculator.",
                        }
                    ],
                    "uncertainty_basis_points": 0,
                    "safe_summary": "Proposed one calculation.",
                }
                if self.calls == 1
                else {
                    "answer_markdown": "4",
                    "tool_proposals": [],
                    "uncertainty_basis_points": 0,
                    "safe_summary": "Answered from the bounded result.",
                }
            )
            return PlannerAttempt(
                raw_output=b"{}",
                report=AgentAttemptReportIn(
                    outcome="succeeded",
                    model_request_id=f"model-{self.calls}",
                    raw_output_sha256=str(self.calls) * 64,
                    usage={
                        "input_tokens": 100,
                        "input_cache_hit_tokens": 0,
                        "input_cache_miss_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                        "cost_microunits": 62,
                        "input_bytes": 1000,
                        "output_bytes": 200,
                        "wall_time_ms": 100,
                    },
                    proposal=proposal,
                    started_at=now,
                    completed_at=now,
                ),
            )

    driver = Phase5AcceptanceDriver(
        config,
        core_transport=httpx.MockTransport(core_handler),
    )
    fake_planner = FakePlanner()
    driver._planner = fake_planner
    async with driver:
        evidence = await driver.run_bounded_wolfram()
    assert fake_planner.calls == 2
    assert evidence["conversation_key"] == CANONICAL_CONVERSATION
    assert evidence["loop_state"] == "complete"
    assert evidence["tool_invocation_count"] == 1
    assert evidence["delivery_intent_count"] == 0
    assert core_calls["GET /v1/agent-tool-loops/agent-loop-bounded"] == 1
