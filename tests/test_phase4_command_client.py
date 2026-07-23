from __future__ import annotations

import importlib.util
from hashlib import sha256
import json
from pathlib import Path
import sys

import httpx
import pytest
from superlily_contracts import load_tool_rollout_plan


MODULE_PATH = (
    Path(__file__).parents[1]
    / "bridges/lily_nonebot/lily_core_bridge/command_rendering.py"
)


def load_module():
    name = "phase4_command_rendering_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _response(request: httpx.Request, status: int, payload=None, content=None):
    return httpx.Response(
        status,
        request=request,
        json=payload if content is None else None,
        content=content,
    )


@pytest.mark.asyncio
async def test_command_client_invokes_exact_tool_then_prepares_verified_delivery() -> None:
    module = load_module()
    png = b"\x89PNG\r\n\x1a\nphase4-command"
    expected_hash = sha256(png).hexdigest()
    observed: list[tuple[str, str, dict | None]] = []
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        body = json.loads(request.content) if request.content else None
        observed.append((request.method, request.url.path, body))
        assert request.headers["authorization"] == "Bearer lily-secret"
        if request.method == "POST" and request.url.path == "/v1/tool-invocations":
            assert body["tool_id"] == "wolfram.run"
            assert body["descriptor_version"] == "1.0.0"
            assert (
                body["descriptor_hash"]
                == module.REVIEWED_TOOLS["wolfram.run"].descriptor_hash
            )
            assert body["principal"]["conversation_id"] == "group:1080353942"
            return _response(
                request,
                201,
                {"invocation_id": "invocation-1", "state": "queued"},
            )
        if request.method == "GET" and request.url.path == "/v1/tool-invocations/invocation-1":
            poll_count += 1
            return _response(
                request,
                200,
                {
                    "invocation_id": "invocation-1",
                    "state": "running" if poll_count == 1 else "succeeded",
                },
            )
        if request.method == "POST" and request.url.path.endswith("/render-result"):
            return _response(
                request,
                201,
                {
                    "artifact_id": "artifact-1",
                    "content_path": "/v1/render-artifacts/artifact-1/content",
                    "content_sha256": expected_hash,
                    "delivery_plan_id": "plan-1",
                    "delivery_plan": {
                        "selected_family": "image",
                        "fallback_text": "计算结果",
                    },
                },
            )
        if request.method == "POST" and request.url.path.endswith("/delivery-intents"):
            return _response(
                request,
                201,
                {
                    "intent_id": "intent-1",
                    "should_send": True,
                    "status": "pending",
                },
            )
        if request.method == "GET" and request.url.path.endswith("/content"):
            return _response(request, 200, content=png)
        if request.method == "POST" and request.url.path.endswith("/complete"):
            assert body == {
                "instance_id": "lily-command",
                "outcome": "succeeded",
                "platform_message_id": "qq-message-42",
                "safe_error_code": None,
            }
            return _response(request, 200, {"outcome": "succeeded"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = module.Phase4CommandClient(
        "http://core",
        "lily-secret",
        poll_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    delivery = await client.prepare_tool_delivery(
        instance_id="lily-command",
        conversation_key="onebot_v11-group_1080353942",
        source_event_id="qq:message:42",
        sender_id="123456",
        platform_roles=["member"],
        tool_id="wolfram.run",
        tool_input={"expression": "Factor[x^2-1]"},
        idempotency_key="phase4-command:test",
    )

    assert delivery.selected_family == "image"
    assert delivery.content == png
    assert delivery.intent_id == "intent-1"
    assert await client.complete_delivery(
        instance_id="lily-command",
        intent_id=delivery.intent_id,
        outcome="succeeded",
        platform_message_id="qq-message-42",
    )
    assert poll_count == 2
    assert any(path.endswith("/delivery-intents") for _, path, _ in observed)


@pytest.mark.asyncio
async def test_nonexecuting_canary_falls_back_before_render_or_send() -> None:
    module = load_module()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _response(
            request,
            201,
            {"invocation_id": "invocation-ledger", "state": "recorded_only"},
        )

    client = module.Phase4CommandClient(
        "http://core",
        "lily-secret",
        poll_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(module.Phase4CommandFallback) as caught:
        await client.prepare_tool_delivery(
            instance_id="lily-command",
            conversation_key="onebot_v11-group_1080353942",
            source_event_id="qq:message:43",
            sender_id="123456",
            platform_roles=["member"],
            tool_id="status.inspect",
            tool_input={"scope": "provider_runtime"},
            idempotency_key="phase4-command:ledger",
        )
    assert caught.value.reason == "invocation_recorded_only"
    assert paths == ["/v1/tool-invocations"]


@pytest.mark.asyncio
async def test_existing_delivery_intent_suppresses_legacy_instead_of_double_send() -> None:
    module = load_module()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/delivery-intents")
        return _response(
            request,
            200,
            {
                "intent_id": "intent-existing",
                "should_send": False,
                "status": "succeeded",
            },
        )

    client = module.Phase4CommandClient(
        "http://core",
        "lily-secret",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(module.Phase4CommandSuppressed) as caught:
        await client._receipt_to_delivery(
            {
                "artifact_id": "artifact-existing",
                "content_path": "/v1/render-artifacts/artifact-existing/content",
                "content_sha256": "a" * 64,
                "delivery_plan_id": "plan-existing",
                "delivery_plan": {"selected_family": "image"},
            },
            instance_id="lily-command",
            idempotency_key="phase4-command:existing",
        )
    assert caught.value.status == "succeeded"


def test_command_idempotency_is_canonical_and_input_sensitive() -> None:
    module = load_module()
    first = module.command_idempotency_key(
        instance_id="lily-command",
        source_event_id="qq:message:44",
        command="latex.render",
        arguments={"latex": "x^2", "unused": 1},
    )
    reordered = module.command_idempotency_key(
        instance_id="lily-command",
        source_event_id="qq:message:44",
        command="latex.render",
        arguments={"unused": 1, "latex": "x^2"},
    )
    changed = module.command_idempotency_key(
        instance_id="lily-command",
        source_event_id="qq:message:44",
        command="latex.render",
        arguments={"latex": "x^3", "unused": 1},
    )
    assert first == reordered
    assert first != changed


def test_reviewed_command_client_and_git_rollout_have_identical_exact_targets() -> None:
    module = load_module()
    source = (
        Path(__file__).parents[1]
        / "registry/rollouts/phase4-command-canary-20260723.json"
    ).read_bytes()
    plan = load_tool_rollout_plan(source).plan

    assert plan.rollback_mode == "ledger_only"
    assert plan.max_invocations == 200
    assert {
        item.canonical_conversation for item in plan.items
    } == {"qq:group:1080353942", "qq:group:861651713"}
    assert {item.caller for item in plan.items} == {"command"}
    assert len(plan.items) == 6
    for item in plan.items:
        reviewed = module.REVIEWED_TOOLS[item.tool_id]
        assert item.descriptor_version == reviewed.descriptor_version
        assert item.descriptor_hash == reviewed.descriptor_hash
