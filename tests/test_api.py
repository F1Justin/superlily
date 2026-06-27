from datetime import datetime, timedelta, timezone
import asyncio

from sqlalchemy import select

from superlily_core.models import EventDecision, EventLink, EventObservation, ResponseRecord, SourceEvent


def event_payload(
    instance_id: str = "lily-command",
    *,
    source_event_id: str = "qq:group:123:message:456",
    conversation_id: str = "123",
    message_id: str = "456",
    text: str | None = "hello",
    occurred_at: datetime | None = None,
    event_type: str = "message",
    references: list[dict] | None = None,
) -> dict:
    bot_id = "985393579" if instance_id == "lily-command" else "2022692714"
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "instance": {
            "instance_id": instance_id,
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": bot_id,
            "role": "command" if instance_id == "lily-command" else "talk",
        },
        "event_type": event_type,
        "conversation": {"id": conversation_id, "type": "group", "name": "Test Group"},
        "sender": {"id": "789", "name": "Tester", "roles": []},
        "message": {
            "id": message_id,
            "text": text,
            "segments": [{"type": "text", "data": {"text": text}}] if text is not None else [],
            "attachments": [],
        },
        "references": references or [],
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
        "raw": {"access_token": "must-not-survive", "url": "https://example.test/a?secret=1"},
        "metadata": {},
    }


async def test_event_ingestion_is_idempotent_and_redacted(client, app) -> None:
    headers = {"Authorization": "Bearer lily-secret", "Idempotency-Key": "stable-event-key"}
    first = await client.post("/v1/events", json=event_payload(), headers=headers)
    second = await client.post("/v1/events", json=event_payload(), headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json()["observation_id"] == second.json()["observation_id"]
    assert second.json()["duplicate"] is True

    async with app.state.database.sessions() as session:
        records = (await session.scalars(select(EventObservation))).all()
        assert len(records) == 1
        assert records[0].raw_json == {
            "access_token": "[REDACTED]",
            "url": "https://example.test/a",
        }


async def test_two_bots_create_two_observations_of_one_source_event(client) -> None:
    lily = await client.post(
        "/v1/events",
        json=event_payload("lily-command"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "lily-observation"},
    )
    nekro = await client.post(
        "/v1/events",
        json=event_payload("nekro-agent"),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "nekro-observation"},
    )
    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    assert lily.json()["observation_id"] != nekro.json()["observation_id"]


async def test_correlated_event_gets_one_shadow_decision(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily = await client.post(
        "/v1/events",
        json=event_payload("lily-command", occurred_at=occurred_at),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-lily"},
    )
    nekro = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:nekro-shadow",
            conversation_id="group_123",
            message_id="456",
            occurred_at=occurred_at + timedelta(seconds=1),
        ),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "decision-nekro"},
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        decisions = (await session.scalars(select(EventDecision))).all()
        assert len(decisions) == 1
        assert decisions[0].source_event_id == lily.json()["source_event_id"]
        assert decisions[0].decision_type == "observe_only"
        assert decisions[0].target_instance_id is None
        assert decisions[0].reason == "ordinary_message"


async def test_two_bots_can_ingest_same_source_event_concurrently(client) -> None:
    lily_request = client.post(
        "/v1/events",
        json=event_payload("lily-command"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "concurrent-lily"},
    )
    nekro_request = client.post(
        "/v1/events",
        json=event_payload("nekro-agent"),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "concurrent-nekro"},
    )
    lily, nekro = await asyncio.gather(lily_request, nekro_request)
    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]


async def test_command_event_records_shadow_decision_for_lily(client, app) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload(text="wf 1+1", message_id="command", source_event_id="qq:group:123:message:command"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-command"},
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.source_event_id == response.json()["source_event_id"]
        assert decision.policy_version == "shadow-v1"
        assert decision.decision_type == "command"
        assert decision.target_instance_id == "lily-command"
        assert decision.confidence == 95
        assert decision.reason.startswith("command_prefix:wf")
        assert decision.features_json["command_prefix"] == "wf"
        assert decision.features_json["matched_command"]["rule_id"] == "lily.wolfram"
        assert decision.features_json["matched_command"]["source_plugin"] == "plugins.wolfram"


async def test_to_me_event_records_shadow_decision_for_nekro_and_debug_views(client) -> None:
    payload = event_payload(
        text="莉莉帮我看看",
        message_id="to-me",
        source_event_id="qq:group:123:message:to-me",
    )
    payload["metadata"] = {"to_me": True}
    response = await client.post(
        "/v1/events",
        json=payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-to-me"},
    )
    assert response.status_code == 201

    denied = await client.get("/v1/decisions/recent")
    allowed = await client.get("/v1/decisions/recent", headers={"Authorization": "Bearer admin-secret"})
    summary = await client.get("/v1/decisions/summary", headers={"Authorization": "Bearer admin-secret"})
    context = await client.get(
        f"/v1/events/{response.json()['source_event_id']}/context",
        headers={"Authorization": "Bearer admin-secret"},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()[0]["source_event_id"] == response.json()["source_event_id"]
    assert allowed.json()[0]["decision_type"] == "talk"
    assert allowed.json()[0]["target_instance_id"] == "nekro-agent"
    assert allowed.json()[0]["reason"] == "summons_talk_bot"
    assert summary.status_code == 200
    assert summary.json()[0]["source_event_id"] == response.json()["source_event_id"]
    assert summary.json()[0]["text_preview"] == "莉莉帮我看看"
    assert summary.json()[0]["decision_type"] == "talk"
    assert summary.json()[0]["target_instance_id"] == "nekro-agent"
    assert "talk -> nekro-agent" in summary.json()[0]["summary"]
    assert context.status_code == 200
    assert context.json()["source_event"]["source_event_id"] == response.json()["source_event_id"]
    assert context.json()["decisions"][0]["decision_type"] == "talk"
    assert context.json()["observations"][0]["observation_id"] == response.json()["observation_id"]


async def test_command_registry_debug_endpoint_is_admin_only(client) -> None:
    denied = await client.get("/v1/command-registry")
    allowed = await client.get("/v1/command-registry", headers={"Authorization": "Bearer admin-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["version"] == "2026-06-27-shadow-seed"
    assert any(rule["id"] == "lily.wolfram" for rule in payload["rules"])
    assert any(rule["id"] == "external.updater.control" and rule["sensitive"] for rule in payload["rules"])


async def test_reply_to_bot_response_records_talk_shadow_decision(client, app) -> None:
    response_payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:985393579:message:bot-parent",
        "instance": event_payload()["instance"],
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "bot-parent",
        "text": "bot says hi",
        "segments": [],
        "attachments": [],
        "success": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    bot_response = await client.post(
        "/v1/responses",
        json=response_payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-bot-parent"},
    )
    assert bot_response.status_code == 201

    event = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:reply-to-bot",
            message_id="reply-to-bot",
            text="继续",
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "bot-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-reply-to-bot"},
    )
    assert event.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.decision_type == "talk"
        assert decision.target_instance_id == "nekro-agent"
        assert decision.reason == "reply_to_bot_response"
        assert decision.features_json["reply_to_bot_response"] is True


async def test_non_message_event_records_ignore_shadow_decision(client, app) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload(
            text=None,
            message_id="notice",
            source_event_id="qq:group:123:notice:recall",
            event_type="notice.group_recall",
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "decision-notice"},
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.decision_type == "ignore"
        assert decision.target_instance_id is None
        assert decision.reason == "non_message_event"


async def test_cross_account_different_message_ids_are_not_correlated(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:lily-456",
        message_id="lily-456",
        occurred_at=occurred_at,
        event_type="message.group.normal",
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:group_123:message:nekro-789",
        conversation_id="group_123",
        message_id="nekro-789",
        occurred_at=occurred_at + timedelta(seconds=1),
    )

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "local-id-lily"},
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "local-id-nekro"},
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] != nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        sources = (await session.scalars(select(SourceEvent))).all()
        observations = (await session.scalars(select(EventObservation))).all()
        assert len(sources) == 2
        assert {item.conversation_id for item in sources} == {"123"}
        assert {item.event_type for item in sources} == {"message"}
        assert {item.reported_source_event_id for item in observations} == {
            lily_payload["source_event_id"],
            nekro_payload["source_event_id"],
        }
        assert {item.platform_message_id for item in observations} == {"lily-456", "nekro-789"}


async def test_same_instance_repeating_text_creates_distinct_source_events(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    for suffix, offset in (("first", 0), ("second", 1)):
        response = await client.post(
            "/v1/events",
            json=event_payload(
                source_event_id=f"qq:group:123:message:{suffix}",
                message_id=suffix,
                occurred_at=occurred_at + timedelta(seconds=offset),
            ),
            headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": f"repeat-{suffix}"},
        )
        assert response.status_code == 201

    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_cross_bot_text_outside_window_is_not_correlated(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily = await client.post(
        "/v1/events",
        json=event_payload(
            "lily-command",
            source_event_id="qq:group:123:message:early",
            message_id="early",
            occurred_at=occurred_at,
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "outside-lily"},
    )
    nekro = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:late",
            conversation_id="group_123",
            message_id="late",
            occurred_at=occurred_at + timedelta(seconds=3),
        ),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "outside-nekro"},
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] != nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_non_text_events_are_not_fuzzily_correlated(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    responses = []
    for instance_id, suffix in (("lily-command", "image-a"), ("nekro-agent", "image-b")):
        responses.append(
            await client.post(
                "/v1/events",
                json=event_payload(
                    instance_id,
                    source_event_id=f"qq:group:123:message:{suffix}",
                    message_id=suffix,
                    text=None,
                    occurred_at=occurred_at,
                ),
                headers={
                    "Authorization": f"Bearer {'lily-secret' if instance_id == 'lily-command' else 'nekro-secret'}",
                    "Idempotency-Key": f"non-text-{suffix}",
                },
            )
        )

    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json()["source_event_id"] != responses[1].json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_event_reply_reference_resolves_to_prior_observation(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    parent = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:parent",
            message_id="parent",
            text="parent message",
            occurred_at=occurred_at,
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "reference-parent"},
    )
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:child",
            message_id="child",
            text="reply message",
            occurred_at=occurred_at + timedelta(seconds=1),
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                    "raw": {
                        "access_token": "must-not-survive",
                        "url": "https://example.test/reference?secret=1",
                    },
                }
            ],
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "reference-child"},
    )

    assert parent.status_code == child.status_code == 201
    async with app.state.database.sessions() as session:
        link = await session.scalar(select(EventLink))
        assert link is not None
        assert link.from_source_event_id == child.json()["source_event_id"]
        assert link.to_source_event_id == parent.json()["source_event_id"]
        assert link.relation_type == "reply_to"
        assert link.target_platform_message_id == "parent"
        assert link.target_conversation_id == "123"
        assert link.resolver_status == "resolved"
        assert link.confidence == 100
        assert link.raw_json == {
            "access_token": "[REDACTED]",
            "url": "https://example.test/reference",
        }


async def test_cross_account_reply_reference_resolves_to_canonical_source_event(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_parent = await client.post(
        "/v1/events",
        json=event_payload(
            "lily-command",
            source_event_id="qq:group:123:message:lily-parent",
            message_id="lily-parent",
            text="shared parent",
            occurred_at=occurred_at,
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "reference-cross-lily"},
    )
    nekro_parent = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:nekro-parent",
            conversation_id="group_123",
            message_id="lily-parent",
            text="shared parent",
            occurred_at=occurred_at + timedelta(seconds=1),
        ),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "reference-cross-nekro"},
    )
    nekro_child = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:nekro-child",
            conversation_id="group_123",
            message_id="nekro-child",
            text="reply from nekro",
            occurred_at=occurred_at + timedelta(seconds=2),
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "lily-parent",
                    "conversation_id": "group_123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "reference-cross-child"},
    )

    assert lily_parent.status_code == nekro_parent.status_code == nekro_child.status_code == 201
    assert lily_parent.json()["source_event_id"] == nekro_parent.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        link = await session.scalar(select(EventLink))
        assert link is not None
        assert link.from_source_event_id == nekro_child.json()["source_event_id"]
        assert link.to_source_event_id == lily_parent.json()["source_event_id"]
        assert link.target_conversation_id == "123"
        assert link.resolver_status == "resolved"


async def test_unresolved_event_reference_is_retained_and_visible_in_admin_view(client, app) -> None:
    event = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:orphan-reply",
            message_id="orphan-reply",
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "missing-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "reference-unresolved"},
    )
    assert event.status_code == 201

    denied = await client.get("/v1/event-links/recent")
    allowed = await client.get("/v1/event-links/recent", headers={"Authorization": "Bearer admin-secret"})
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()[0]["from_source_event_id"] == event.json()["source_event_id"]
    assert allowed.json()[0]["to_source_event_id"] is None
    assert allowed.json()[0]["resolver_status"] == "unresolved"
    assert allowed.json()[0]["target_platform_message_id"] == "missing-parent"

    async with app.state.database.sessions() as session:
        link = await session.scalar(select(EventLink))
        assert link is not None
        assert link.resolver_status == "unresolved"


async def test_response_trigger_resolves_reported_id_to_canonical_event(client, app) -> None:
    reported_source_id = "qq:group:123:message:account-local-trigger"
    event = await client.post(
        "/v1/events",
        json=event_payload(source_event_id=reported_source_id, message_id="account-local-trigger"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "trigger-event"},
    )
    assert event.status_code == 201

    payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:985393579:message:trigger-response",
        "instance": event_payload()["instance"],
        "trigger_source_event_id": reported_source_id,
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "trigger-response",
        "text": "done",
        "segments": [],
        "attachments": [],
        "success": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    response = await client.post(
        "/v1/responses",
        json=payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "trigger-response"},
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ResponseRecord))
        assert record is not None
        assert record.trigger_source_event_id == event.json()["source_event_id"]


async def test_instance_token_cannot_impersonate_other_instance(client) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload("nekro-agent"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "wrong-identity"},
    )
    assert response.status_code == 403


async def test_response_may_have_no_trigger_event(client, app) -> None:
    payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:985393579:message:999",
        "instance": event_payload()["instance"],
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "999",
        "text": "scheduled message",
        "segments": [],
        "attachments": [],
        "success": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    response = await client.post(
        "/v1/responses",
        json=payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "response-without-trigger"},
    )
    assert response.status_code == 201, response.text
    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ResponseRecord))
        assert record is not None
        assert record.trigger_observation_id is None
        assert record.trigger_source_event_id is None


async def test_heartbeat_and_admin_views(client) -> None:
    now = datetime.now(timezone.utc).isoformat()
    heartbeat = {
        "schema_version": "1.0",
        "instance": event_payload()["instance"],
        "process_status": "running",
        "connection_status": "connected",
        "occurred_at": now,
        "metadata": {"queue_depth": 0},
    }
    sent = await client.post(
        "/v1/heartbeats",
        json=heartbeat,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert sent.status_code == 200, sent.text

    denied = await client.get("/v1/instances")
    allowed = await client.get("/v1/instances", headers={"Authorization": "Bearer admin-secret"})
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()[0]["status"] == "online"
