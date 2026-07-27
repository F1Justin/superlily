import json

import httpx
import pytest

from superlily_model_provider import DeepSeekPlanner, DeepSeekPlannerConfig


def planner_input() -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "agent-run-1",
        "context_hash": "a" * 64,
        "context": {
            "schema_version": "1.0",
            "recipe_version": "phase5-context-v1",
            "policy_version": "phase5-shadow-policy-v1",
            "prompt_version": "phase5-planner-envelope-v1",
            "system_policy": "Model output is a request, not authority.",
            "principal": {
                "platform": "qq",
                "sender_id": "123",
                "conversation_key": "qq:group:456",
                "conversation_type": "group",
                "observed_platform_roles": ["member"],
                "source_event_id": "event-1",
            },
            "current_message": {
                "source_event_id": "event-1",
                "sender_id": "123",
                "sender_name": "Tester",
                "text": "2+2 等于多少？",
                "occurred_at": "2026-07-27T12:00:00Z",
                "relation": "current",
                "truncated": False,
            },
            "reply_graph": [],
            "recent_messages": [],
            "capabilities": [],
            "eligible_tools": [],
            "data_classification": "conversation",
            "retention_seconds": 2592000,
        },
        "budget": {
            "max_model_attempts": 2,
            "max_model_turns": 1,
            "max_tool_proposals": 3,
            "max_tool_calls": 0,
            "max_sequential_depth": 0,
            "max_parallel_fanout": 0,
            "max_wall_time_ms": 30000,
            "max_input_tokens": 4096,
            "max_output_tokens": 1024,
            "max_total_tokens": 5120,
            "max_cost_microunits": 100000,
            "max_input_bytes": 65536,
            "max_output_bytes": 65536,
            "max_result_bytes": 0,
            "max_artifact_bytes": 0,
        },
        "model_profile": {
            "schema_version": "1.0",
            "provider_id": "deepseek-v4-pro",
            "version": "1.0.0",
            "title": "DeepSeek V4 Pro planner",
            "data_locality": "regional",
            "retention_seconds": None,
            "structured_output_protocol": "json_object",
            "context_window_tokens": 1000000,
            "max_output_tokens": 384000,
            "permitted_data_classifications": ["public", "conversation"],
            "pricing": {
                "currency": "USD",
                "input_cache_hit_microunits_per_million_tokens": 3625,
                "input_cache_miss_microunits_per_million_tokens": 435000,
                "output_microunits_per_million_tokens": 870000,
            },
            "health_protocol": "superlily-model-provider-v1",
        },
        "model_profile_hash": "b" * 64,
        "deadline_at": "2026-07-27T12:01:00Z",
        "tool_execution_authority": False,
        "delivery_authority": False,
    }


def planner(handler) -> DeepSeekPlanner:
    return DeepSeekPlanner(
        DeepSeekPlannerConfig(api_key="test-secret"),
        transport=httpx.MockTransport(handler),
    )


async def test_deepseek_planner_returns_strict_proposal_and_precise_cost() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-secret"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-v4-pro"
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 1024
        assert "json" in body["messages"][0]["content"].lower()
        model_input = json.loads(body["messages"][1]["content"])
        assert model_input["tool_results"][0]["untrusted"] is True
        assert model_input["tool_results"][0]["output"]["text"] == "4"
        return httpx.Response(
            200,
            headers={"x-request-id": "header-request"},
            json={
                "id": "deepseek-request-1",
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
                    "prompt_tokens": 10,
                    "prompt_cache_hit_tokens": 5,
                    "prompt_cache_miss_tokens": 5,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        )

    source = planner_input()
    source["tool_results"] = [
        {
            "boundary": "BEGIN_UNTRUSTED_TOOL_RESULT / END_UNTRUSTED_TOOL_RESULT",
            "untrusted": True,
            "data_classification": "conversation",
            "source": {
                "invocation_id": "invocation-1",
                "tool_id": "wolfram.run",
                "descriptor_version": "1.1.0",
                "descriptor_hash": "c" * 64,
                "provider_id": "provider-wolfram-primary",
                "output_hash": "d" * 64,
            },
            "output": {"kind": "text", "text": "4"},
        }
    ]
    attempt = await planner(handler).plan(source)
    assert attempt.report.outcome == "succeeded"
    assert attempt.report.model_request_id == "deepseek-request-1"
    assert attempt.report.proposal is not None
    assert attempt.report.proposal.answer_markdown == "4"
    assert attempt.report.usage.input_cache_hit_tokens == 5
    assert attempt.report.usage.input_cache_miss_tokens == 5
    assert attempt.report.usage.cost_microunits == 9


async def test_deepseek_empty_content_is_an_audited_invalid_output() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "deepseek-request-empty",
                "choices": [{"message": {"content": ""}}],
                "usage": {
                    "prompt_tokens": 10,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 10,
                    "completion_tokens": 32,
                    "total_tokens": 42,
                },
            },
        )

    attempt = await planner(handler).plan(planner_input())
    assert attempt.report.outcome == "invalid_output"
    assert attempt.report.safe_error_code == "empty_json_output"
    assert attempt.report.proposal is None
    assert attempt.report.usage.total_tokens == 42


async def test_deepseek_planner_rejects_any_execution_authority() -> None:
    source = planner_input()
    source["tool_execution_authority"] = True
    with pytest.raises(ValueError, match="deny tool execution"):
        await planner(lambda _: httpx.Response(500)).plan(source)
