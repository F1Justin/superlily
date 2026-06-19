from datetime import datetime, timezone
import asyncio

from sqlalchemy import select

from superlily_core.models import EventObservation, ResponseRecord


def event_payload(instance_id: str = "lily-command") -> dict:
    bot_id = "985393579" if instance_id == "lily-command" else "2022692714"
    return {
        "schema_version": "1.0",
        "source_event_id": "qq:group:123:message:456",
        "instance": {
            "instance_id": instance_id,
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": bot_id,
            "role": "command" if instance_id == "lily-command" else "talk",
        },
        "event_type": "message",
        "conversation": {"id": "123", "type": "group", "name": "Test Group"},
        "sender": {"id": "789", "name": "Tester", "roles": []},
        "message": {
            "id": "456",
            "text": "hello",
            "segments": [{"type": "text", "data": {"text": "hello"}}],
            "attachments": [],
        },
        "occurred_at": datetime.now(timezone.utc).isoformat(),
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
