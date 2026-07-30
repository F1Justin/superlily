from dataclasses import replace
from datetime import datetime, timezone
import asyncio

import pytest
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from superlily_contracts import ModelProviderProfile, model_profile_hash
from superlily_core.agent_product_service import advance_agent_product
from superlily_core.agent_run_service import import_model_profile
from superlily_core.models import (
    AgentInteraction,
    AgentRun,
    AgentTextDeliveryEvent,
    AgentTextDeliveryIntent,
)


MODEL_TOKEN = "model-product-secret"
TRIGGER_TOKEN = "trigger-product-secret-with-32-bytes"


def profile() -> ModelProviderProfile:
    return ModelProviderProfile(
        provider_id="deepseek-v4-pro",
        version="1.0.0",
        title="DeepSeek product test",
        data_locality="regional",
        retention_seconds=0,
        structured_output_protocol="json_object",
        context_window_tokens=131_072,
        max_output_tokens=8_192,
        permitted_data_classifications=["conversation"],
        pricing={
            "currency": "USD",
            "input_cache_hit_microunits_per_million_tokens": 1_000_000,
            "input_cache_miss_microunits_per_million_tokens": 1_000_000,
            "output_microunits_per_million_tokens": 1_000_000,
        },
        health_protocol="superlily-model-provider-v1",
    )


def event_payload(
    *,
    source_event_id: str = "qq:group:708309706:message:product-1",
    conversation_id: str = "708309706",
    is_tome: bool = True,
) -> dict:
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
        "conversation": {
            "id": conversation_id,
            "type": "group",
            "name": "Agent Product Test",
        },
        "sender": {"id": "2843657817", "name": "Tester", "roles": []},
        "message": {
            "id": "product-message-1",
            "text": "莉莉，直接回答一句测试",
            "segments": [{"type": "text", "data": {"text": "莉莉，直接回答一句测试"}}],
            "attachments": [],
        },
        "references": [],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "is_tome": is_tome,
            "chat_key": f"onebot_v11-group_{conversation_id}",
            "agent_trigger_kind": "mention",
        },
    }


def headers(key: str = "agent-product-entry-1") -> dict[str, str]:
    return {
        "Authorization": "Bearer nekro-secret",
        "Idempotency-Key": key,
    }


async def enable_product(app) -> str:
    active_profile = profile()
    profile_hash = model_profile_hash(active_profile)
    app.state.settings = replace(
        app.state.settings,
        agent_mode="bounded_readonly",
        model_provider_tokens={"deepseek-v4-pro": MODEL_TOKEN},
        agent_product_mode="canary",
        agent_canary_conversations=frozenset({"qq:group:708309706"}),
        agent_entry_instances=frozenset({"nekro-agent"}),
        agent_model_provider_id="deepseek-v4-pro",
        agent_model_profile_version="1.0.0",
        agent_provider_trigger_url="http://deepseek-model-provider:8010",
        agent_provider_trigger_token=TRIGGER_TOKEN,
    )
    async with app.state.database.sessions() as session:
        await import_model_profile(
            session,
            active_profile,
            source_commit="1" * 40,
            bundle_hash=profile_hash,
            reviewer="agent-product-test",
        )
    return profile_hash


async def test_product_entry_is_exact_addressed_and_idempotent(client, app) -> None:
    await enable_product(app)
    rejected = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(
            source_event_id="qq:group:708309706:message:not-addressed",
            is_tome=False,
        ),
        headers=headers("agent-product-not-addressed"),
    )
    assert rejected.status_code == 200
    assert rejected.json()["accepted"] is False
    assert rejected.json()["reason_code"] == "not_explicitly_addressed"

    wrong_group = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(
            source_event_id="qq:group:123:message:wrong-group",
            conversation_id="123",
        ),
        headers=headers("agent-product-wrong-group"),
    )
    assert wrong_group.status_code == 200
    assert wrong_group.json()["reason_code"] == "conversation_not_allowed"

    first = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(),
        headers=headers(),
    )
    assert first.status_code == 202
    assert first.json()["accepted"] is True
    duplicate = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(),
        headers=headers(),
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["interaction_id"] == first.json()["interaction_id"]
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(AgentInteraction.id))) == 1


async def test_product_entry_has_conversation_concurrency_and_rate_fuses(
    client,
    app,
) -> None:
    await enable_product(app)
    first = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(),
        headers=headers(),
    )
    assert first.status_code == 202
    busy = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(
            source_event_id="qq:group:708309706:message:product-busy",
        ),
        headers=headers("agent-product-busy"),
    )
    assert busy.status_code == 200
    assert busy.json()["reason_code"] == "conversation_busy"

    app.state.settings = replace(
        app.state.settings,
        agent_max_concurrent_per_conversation=2,
        agent_max_interactions_per_window=1,
    )
    limited = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(
            source_event_id="qq:group:708309706:message:product-rate",
        ),
        headers=headers("agent-product-rate"),
    )
    assert limited.status_code == 200
    assert limited.json()["reason_code"] == "conversation_rate_limited"


async def test_product_admission_serializes_concurrent_messages(client, app) -> None:
    await enable_product(app)
    first_payload = event_payload(
        source_event_id="qq:group:708309706:message:product-race-1",
    )
    first_payload["message"]["id"] = "product-race-message-1"
    first_payload["message"]["text"] = "莉莉，并发测试甲"
    first_payload["message"]["segments"][0]["data"]["text"] = "莉莉，并发测试甲"
    second_payload = event_payload(
        source_event_id="qq:group:708309706:message:product-race-2",
    )
    second_payload["message"]["id"] = "product-race-message-2"
    second_payload["message"]["text"] = "莉莉，并发测试乙"
    second_payload["message"]["segments"][0]["data"]["text"] = "莉莉，并发测试乙"

    first, second = await asyncio.gather(
        client.post(
            "/v1/agent-interactions/evaluate",
            json=first_payload,
            headers=headers("agent-product-race-1"),
        ),
        client.post(
            "/v1/agent-interactions/evaluate",
            json=second_payload,
            headers=headers("agent-product-race-2"),
        ),
    )

    responses = [first, second]
    assert sorted(response.status_code for response in responses) == [200, 202]
    assert sorted(response.json()["reason_code"] for response in responses) == [
        "conversation_busy",
        "exact_canary_accepted",
    ]
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count(AgentInteraction.id))) == 1


async def test_direct_answer_gets_one_fenced_native_delivery(client, app) -> None:
    await enable_product(app)
    accepted = await client.post(
        "/v1/agent-interactions/evaluate",
        json=event_payload(),
        headers=headers(),
    )
    assert accepted.status_code == 202
    await advance_agent_product(app.state.database, app.state.settings)
    async with app.state.database.sessions() as session:
        interaction = await session.get(
            AgentInteraction,
            accepted.json()["interaction_id"],
        )
        assert interaction is not None and interaction.state == "planning"
        run = await session.get(AgentRun, interaction.run_id)
        assert run is not None and run.creator_type == "system"
        run_id = run.id

    now = datetime.now(timezone.utc).isoformat()
    attempt = await client.post(
        f"/v1/agent-runs/{run_id}/attempts",
        headers={
            "Authorization": f"Bearer {MODEL_TOKEN}",
            "Idempotency-Key": "agent-product-model-attempt",
        },
        json={
            "schema_version": "1.0",
            "outcome": "succeeded",
            "model_request_id": "deepseek-product-1",
            "raw_output_sha256": "a" * 64,
            "usage": {
                "input_tokens": 10,
                "input_cache_hit_tokens": 0,
                "input_cache_miss_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
                "cost_microunits": 15,
                "input_bytes": 100,
                "output_bytes": 50,
                "wall_time_ms": 50,
            },
            "proposal": {
                "answer_markdown": "这是 Core Agent 的一次直接回答。",
                "tool_proposals": [],
                "uncertainty_basis_points": 100,
                "safe_summary": "Direct answer.",
            },
            "safe_error_code": None,
            "started_at": now,
            "completed_at": now,
        },
    )
    assert attempt.status_code == 201, attempt.text
    await advance_agent_product(app.state.database, app.state.settings)

    lease = await client.post(
        "/v1/agent-text-deliveries/lease",
        headers={"Authorization": "Bearer nekro-secret"},
        json={"schema_version": "1.0", "instance_id": "nekro-agent"},
    )
    assert lease.status_code == 200
    lease_body = lease.json()
    assert lease_body["conversation_key"] == "qq:group:708309706"
    assert lease_body["reply_to_platform_message_id"] == "product-message-1"
    assert lease_body["text"] == "这是 Core Agent 的一次直接回答。"
    assert lease_body["fence"] == 1

    second_lease = await client.post(
        "/v1/agent-text-deliveries/lease",
        headers={"Authorization": "Bearer nekro-secret"},
        json={"schema_version": "1.0", "instance_id": "nekro-agent"},
    )
    assert second_lease.status_code == 204
    bad_receipt = await client.post(
        f"/v1/agent-text-deliveries/{lease_body['intent_id']}/complete",
        headers={"Authorization": "Bearer nekro-secret"},
        json={
            "schema_version": "1.0",
            "instance_id": "nekro-agent",
            "fence": 1,
            "lease_token": "wrong-token-that-is-long-enough-000",
            "outcome": "succeeded",
            "platform_message_id": "qq-message-1",
            "safe_error_code": None,
        },
    )
    assert bad_receipt.status_code == 409
    completed = await client.post(
        f"/v1/agent-text-deliveries/{lease_body['intent_id']}/complete",
        headers={"Authorization": "Bearer nekro-secret"},
        json={
            "schema_version": "1.0",
            "instance_id": "nekro-agent",
            "fence": lease_body["fence"],
            "lease_token": lease_body["lease_token"],
            "outcome": "succeeded",
            "platform_message_id": "qq-message-1",
            "safe_error_code": None,
        },
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "succeeded"
    duplicate = await client.post(
        f"/v1/agent-text-deliveries/{lease_body['intent_id']}/complete",
        headers={"Authorization": "Bearer nekro-secret"},
        json={
            "schema_version": "1.0",
            "instance_id": "nekro-agent",
            "fence": lease_body["fence"],
            "lease_token": lease_body["lease_token"],
            "outcome": "succeeded",
            "platform_message_id": "qq-message-1",
            "safe_error_code": None,
        },
    )
    assert duplicate.status_code == 200
    async with app.state.database.sessions() as session:
        interaction = await session.get(
            AgentInteraction,
            accepted.json()["interaction_id"],
        )
        intent = await session.get(AgentTextDeliveryIntent, lease_body["intent_id"])
        assert interaction is not None and interaction.state == "succeeded"
        assert intent is not None and intent.state == "succeeded"
        intent_id = intent.id
        assert (
            await session.scalar(
                select(func.count(AgentTextDeliveryEvent.id)).where(
                    AgentTextDeliveryEvent.intent_id == intent.id
                )
            )
            == 3
        )
        with pytest.raises(DBAPIError):
            await session.execute(
                update(AgentInteraction)
                .where(AgentInteraction.id == interaction.id)
                .values(state="delivery_pending", terminal_at=None)
            )
            await session.commit()
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(
                delete(AgentTextDeliveryEvent).where(
                    AgentTextDeliveryEvent.intent_id == intent_id
                )
            )
            await session.commit()
