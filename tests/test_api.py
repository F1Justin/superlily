from datetime import datetime, timedelta, timezone
import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import delete, select

from superlily_core.command_registry import runtime_registry_snapshot_hash
from superlily_core.models import (
    CollectorWatermark,
    ConversationCaptureProfile,
    ConversationNameObservation,
    EventClaim,
    EventDecision,
    EventLink,
    EventObservation,
    IngressReceiptRecord,
    IdentityNameObservation,
    PlatformActionObservation,
    ResponseRecord,
    SourceEvent,
)


def event_payload(
    instance_id: str = "lily-command",
    *,
    source_event_id: str = "qq:group:123:message:456",
    conversation_id: str = "123",
    message_id: str = "456",
    text: str | None = "hello",
    real_seq: str | None = None,
    occurred_at: datetime | None = None,
    event_type: str = "message",
    references: list[dict] | None = None,
) -> dict:
    bot_id = "985393579" if instance_id == "lily-command" else "2022692714"
    active_occurred_at = occurred_at or datetime.now(timezone.utc)
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
        "occurred_at": active_occurred_at.isoformat(),
        "raw": {
            "access_token": "must-not-survive",
            "url": "https://example.test/a?secret=1",
        },
        "metadata": {
            "native_identity": {
                "schema": "onebot_v11.qq.native_identity.v1",
                "message_id": message_id,
                "real_seq": real_seq or message_id,
                "group_id": conversation_id.removeprefix("group_"),
                "user_id": "789",
                "time": str(int(active_occurred_at.timestamp())),
            }
        },
    }


def response_payload(
    instance_id: str = "lily-command",
    *,
    source_response_id: str,
    platform_message_id: str | None,
    conversation_id: str = "123",
    occurred_at: datetime | None = None,
    trigger_observation_id: str | None = None,
    trigger_source_event_id: str | None = None,
    success: bool = True,
) -> dict:
    return {
        "schema_version": "1.0",
        "source_response_id": source_response_id,
        "instance": event_payload(instance_id)["instance"],
        "trigger_observation_id": trigger_observation_id,
        "trigger_source_event_id": trigger_source_event_id,
        "response_type": "message",
        "conversation": {"id": conversation_id, "type": "group"},
        "platform_message_id": platform_message_id,
        "text": "response",
        "segments": [],
        "attachments": [],
        "success": success,
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
    }


async def test_event_ingestion_is_idempotent_and_redacted(client, app) -> None:
    headers = {
        "Authorization": "Bearer lily-secret",
        "Idempotency-Key": "stable-event-key",
    }
    payload = event_payload()
    payload["message"]["segments"].append(
        {
            "type": "json",
            "data": {"jumpUrl": "mqqapi://user:password@qzoneschema/feed?token=secret#fragment"},
        }
    )
    first = await client.post("/v1/events", json=payload, headers=headers)
    second = await client.post("/v1/events", json=payload, headers=headers)

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
        assert records[0].segments_json[1]["data"]["jumpUrl"] == "mqqapi://qzoneschema/feed"


async def test_name_history_tracks_qq_identity_across_groups_and_time(client, app) -> None:
    started = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)

    async def ingest(
        sequence: int,
        *,
        conversation_id: str,
        conversation_name: str,
        account_name: str,
        display_name: str,
    ) -> None:
        payload = event_payload(
            source_event_id=f"qq:source:v2:{sequence:064x}",
            conversation_id=conversation_id,
            message_id=f"name-history-{sequence}",
            real_seq=f"name-history-real-{sequence}",
            occurred_at=started + timedelta(minutes=sequence),
        )
        payload["conversation"]["name"] = conversation_name
        payload["sender"] = {
            "id": "12345678",
            "account_name": account_name,
            "display_name": display_name,
            "name": display_name,
            "roles": [],
        }
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"name-history-{sequence}",
            },
        )
        assert response.status_code == 201, response.text

    await ingest(
        1,
        conversation_id="10001",
        conversation_name="Group One",
        account_name="Account A",
        display_name="Card A",
    )
    await ingest(
        2,
        conversation_id="10002",
        conversation_name="Group Two",
        account_name="Account A",
        display_name="Card B",
    )
    await ingest(
        3,
        conversation_id="10001",
        conversation_name="Group One Renamed",
        account_name="Account B",
        display_name="Card A Renamed",
    )

    async with app.state.database.sessions() as session:
        identity = (
            await session.scalars(
                select(IdentityNameObservation)
                .where(IdentityNameObservation.user_id == "12345678")
                .order_by(IdentityNameObservation.observed_at)
            )
        ).all()
        conversations = (
            await session.scalars(select(ConversationNameObservation).order_by(ConversationNameObservation.observed_at))
        ).all()

    assert [(row.name_kind, row.name_value, row.conversation_id) for row in identity] == [
        ("account_name", "Account A", "10001"),
        ("conversation_display_name", "Card A", "10001"),
        ("conversation_display_name", "Card B", "10002"),
        ("account_name", "Account B", "10001"),
        ("conversation_display_name", "Card A Renamed", "10001"),
    ]
    assert [(row.conversation_id, row.name_value) for row in conversations] == [
        ("10001", "Group One"),
        ("10002", "Group Two"),
        ("10001", "Group One Renamed"),
    ]


async def test_c0d_action_capture_receipt_and_replay_are_idempotent(client, app) -> None:
    target = event_payload(
        source_event_id="qq:source:v2:" + "1" * 64,
        message_id="target-message",
        real_seq="target-real-seq",
    )
    target_response = await client.post(
        "/v1/events",
        json=target,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-target",
        },
    )
    assert target_response.status_code == 201, target_response.text

    occurred_at = datetime.now(timezone.utc)
    reaction = event_payload(
        source_event_id="qq:reaction:" + "2" * 64,
        message_id="reaction-notice",
        real_seq="reaction-notice",
        event_type="notice.group_msg_emoji_like",
        occurred_at=occurred_at,
    )
    reaction["message"] = None
    reaction["ingress"] = {
        "spool_id": "lily-main",
        "sequence": 1,
        "record_sha256": "3" * 64,
        "captured_at": occurred_at.isoformat(),
    }
    reaction["capture"] = {
        "status": "partial",
        "sanitizer_version": "superlily.sanitizer.v1",
        "original_payload_sha256": "4" * 64,
        "original_payload_size_bytes": 1234,
        "omitted_fields": ["image.url"],
        "platform_extra": {
            "sub_type": "add",
            "callback_url": "https://example.test/action?token=secret",
        },
        "reason": "image bytes excluded",
    }
    reaction["actions"] = [
        {
            "action_kind": "reaction",
            "operation": "add",
            "actor_principal_id": "owner",
            "subject_principal_id": "bot",
            "target_platform_message_id": "target-message",
            "value": {
                "emoji_id": "128074",
                "count": 1,
                "jump_url": "https://example.test/reaction?ticket=secret",
            },
            "capture_status": "complete",
        }
    ]
    headers = {
        "Authorization": "Bearer lily-secret",
        "Idempotency-Key": "c0d-reaction-1",
    }
    first = await client.post("/v1/events", json=reaction, headers=headers)
    replay = await client.post("/v1/events", json=reaction, headers=headers)

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["receipt_id"] == replay.json()["receipt_id"]
    assert first.json()["outcome"] == "committed"
    assert replay.json()["outcome"] == "duplicate"
    assert first.json()["highest_contiguous_sequence"] == 1
    assert first.json()["highest_seen_sequence"] == 1

    async with app.state.database.sessions() as session:
        observation = await session.get(EventObservation, first.json()["observation_id"])
        assert observation is not None
        assert observation.capture_profile == "operational"
        assert observation.capture_status == "partial"
        assert observation.platform_extra_json == {
            "sub_type": "add",
            "callback_url": "https://example.test/action",
        }
        receipts = (await session.scalars(select(IngressReceiptRecord))).all()
        actions = (await session.scalars(select(PlatformActionObservation))).all()
        assert len(receipts) == 2  # The pre-spool target also receives a generic receipt.
        assert len(actions) == 1
        assert actions[0].target_source_event_id == target_response.json()["source_event_id"]
        assert actions[0].resolver_status == "resolved"
        assert actions[0].value_json == {
            "emoji_id": "128074",
            "count": 1,
            "jump_url": "https://example.test/reaction",
        }

    watermark_response = await client.get(
        "/v1/ingress/watermarks",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert watermark_response.status_code == 200
    assert watermark_response.json() == [
        {
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "spool_id": "lily-main",
            "highest_contiguous_sequence": 1,
            "highest_seen_sequence": 1,
            "next_gap_sequence": None,
            "last_receipt_at": watermark_response.json()[0]["last_receipt_at"],
            "updated_at": watermark_response.json()[0]["updated_at"],
        }
    ]

    recent = await client.get(
        "/v1/events/recent",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert recent.status_code == 200
    reaction_view = next(item for item in recent.json() if item["observation_id"] == first.json()["observation_id"])
    assert datetime.fromisoformat(reaction_view["occurred_at"]).replace(tzinfo=timezone.utc) == occurred_at
    assert datetime.fromisoformat(reaction_view["ingress"]["captured_at"]).replace(tzinfo=timezone.utc) == occurred_at
    assert reaction_view["ingress"]["committed_at"] is not None
    assert reaction_view["ingress"]["sequence"] == 1

    changed = {
        **reaction,
        "actions": [{**reaction["actions"][0], "value": {"emoji_id": "9"}}],
    }
    conflict = await client.post("/v1/events", json=changed, headers=headers)
    assert conflict.status_code == 409


async def test_c0d_watermark_exposes_and_closes_gaps(client, app) -> None:
    async def ingest(sequence: int) -> dict:
        occurred_at = datetime.now(timezone.utc)
        payload = event_payload(
            source_event_id=f"qq:source:v2:{sequence:064x}",
            message_id=f"c0d-message-{sequence}",
            real_seq=f"c0d-real-{sequence}",
            occurred_at=occurred_at,
        )
        payload["ingress"] = {
            "spool_id": "gap-spool",
            "sequence": sequence,
            "record_sha256": f"{sequence:064x}",
            "captured_at": occurred_at.isoformat(),
        }
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"c0d-gap-{sequence}",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    assert (await ingest(1))["highest_contiguous_sequence"] == 1
    third = await ingest(3)
    assert third["highest_contiguous_sequence"] == 1
    assert third["highest_seen_sequence"] == 3
    second = await ingest(2)
    assert second["highest_contiguous_sequence"] == 3
    assert second["highest_seen_sequence"] == 3

    async with app.state.database.sessions() as session:
        watermark = await session.get(CollectorWatermark, ("lily-command", "gap-spool"))
        assert watermark is not None
        assert watermark.highest_contiguous_sequence == 3
        assert watermark.highest_seen_sequence == 3

    collision_payload = event_payload(
        source_event_id="qq:source:v2:" + "f" * 64,
        message_id="c0d-collision",
        real_seq="c0d-collision",
    )
    collision_payload["ingress"] = {
        "spool_id": "gap-spool",
        "sequence": 2,
        "record_sha256": "f" * 64,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    collision = await client.post(
        "/v1/events",
        json=collision_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-gap-collision",
        },
    )
    assert collision.status_code == 409


async def test_c0d_user_target_action_persists_without_message_target(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    payload = event_payload(
        source_event_id="qq:poke:" + "e" * 64,
        message_id="poke-notice",
        real_seq="poke-notice",
        event_type="notice.notify.poke",
        occurred_at=occurred_at,
    )
    payload["message"] = None
    payload["actions"] = [
        {
            "action_kind": "poke",
            "operation": "observed_state",
            "actor_principal_id": "42",
            "subject_principal_id": "43",
            "value": {"sub_type": "poke"},
        }
    ]
    response = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-poke-subject",
        },
    )
    assert response.status_code == 201, response.text

    async with app.state.database.sessions() as session:
        action = await session.scalar(
            select(PlatformActionObservation).where(
                PlatformActionObservation.observation_id == response.json()["observation_id"]
            )
        )
        assert action is not None
        assert action.action_kind == "poke"
        assert action.subject_principal_id == "43"
        assert action.target_platform_message_id is None
        assert action.resolver_status == "unavailable"


async def test_c0d_ingress_status_reconciles_bridge_and_core_watermarks(client) -> None:
    occurred_at = datetime.now(timezone.utc)
    payload = event_payload(
        source_event_id="qq:diagnostic:" + "d" * 64,
        message_id="diagnostic-message",
        real_seq="diagnostic-message",
        occurred_at=occurred_at,
    )
    payload["ingress"] = {
        "spool_id": "diagnostic-spool",
        "sequence": 1,
        "record_sha256": "d" * 64,
        "captured_at": occurred_at.isoformat(),
    }
    event = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-diagnostic-event",
        },
    )
    assert event.status_code == 201, event.text

    heartbeat = {
        "instance": payload["instance"],
        "process_status": "running",
        "connection_status": "connected",
        "occurred_at": occurred_at.isoformat(),
        "ingress_spool": {
            "state": "pending",
            "durability_mode": "sqlite_full",
            "spool_id": "diagnostic-spool",
            "pending_records": 1,
            "pending_bytes": 2048,
            "committed_records": 1,
            "quarantined_records": 0,
            "quarantined_files": 0,
            "oldest_pending_seconds": 2.0,
            "live_bytes": 8192,
            "quota_bytes": 268_435_456,
            "highest_sequence": 2,
            "replay_successes": 1,
            "replay_failures": 0,
            "capture_failures": 0,
            "quota_rejections": 0,
            "observed_at": occurred_at.isoformat(),
        },
    }
    sent = await client.post(
        "/v1/heartbeats",
        json=heartbeat,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert sent.status_code == 200, sent.text
    assert (await client.get("/v1/ingress/status")).status_code == 401
    status_response = await client.get(
        "/v1/ingress/status",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert status_response.status_code == 200
    status = status_response.json()[0]
    assert status["instance_id"] == "lily-command"
    assert status["spool"]["pending_records"] == 1
    assert status["core_watermark"]["highest_contiguous_sequence"] == 1
    assert status["core_watermark"]["highest_seen_sequence"] == 1
    assert status["reconciliation"] == {
        "state": "pending",
        "lag_records": 1,
        "next_gap_sequence": None,
    }
    instances = await client.get(
        "/v1/instances",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert instances.json()[0]["ingress_spool"]["spool_id"] == "diagnostic-spool"


async def test_c0d_claim_ingest_returns_receipt_and_late_action_target_resolves(
    client,
    app,
) -> None:
    target_time = datetime.now(timezone.utc)
    action_time = target_time + timedelta(seconds=1)
    action_payload = event_payload(
        source_event_id="qq:late-action:" + "a" * 64,
        message_id="late-action-notice",
        real_seq="late-action-notice",
        event_type="notice.group_msg_emoji_like",
        occurred_at=action_time,
    )
    action_payload["message"] = None
    action_payload["ingress"] = {
        "spool_id": "claim-spool",
        "sequence": 1,
        "record_sha256": "a" * 64,
        "captured_at": action_time.isoformat(),
    }
    action_payload["actions"] = [
        {
            "action_kind": "reaction",
            "operation": "add",
            "actor_principal_id": "owner",
            "target_platform_message_id": "late-target-message",
            "value": {"emoji_id": "1"},
        }
    ]
    claim = await client.post(
        "/v1/claims/evaluate",
        json=action_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-claim-action",
        },
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["ingest_receipt"]["spool_id"] == "claim-spool"
    assert claim.json()["ingest_receipt"]["sequence"] == 1
    assert claim.json()["ingest_receipt"]["outcome"] == "committed"

    claim_replay = await client.post(
        "/v1/claims/evaluate",
        json=action_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-claim-action",
        },
    )
    assert claim_replay.status_code == 200, claim_replay.text
    assert claim_replay.json()["duplicate"] is True
    assert claim_replay.json()["ingest_receipt"]["outcome"] == "duplicate"
    assert claim_replay.json()["ingest_receipt"]["receipt_id"] == claim.json()["ingest_receipt"]["receipt_id"]

    async with app.state.database.sessions() as session:
        action = await session.scalar(select(PlatformActionObservation))
        assert action is not None
        assert action.resolver_status == "unresolved"
        session.add(
            ConversationCaptureProfile(
                platform="qq",
                conversation_type="group",
                conversation_id="123",
                capture_profile="operational",
                retention_class="operational",
                policy_version="group-policy-v2",
                source_commit="b" * 40,
            )
        )
        await session.commit()

    target_payload = event_payload(
        source_event_id="qq:source:v2:" + "b" * 64,
        message_id="late-target-message",
        real_seq="late-target-real-seq",
        occurred_at=target_time,
    )
    target = await client.post(
        "/v1/events",
        json=target_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "c0d-late-target",
        },
    )
    assert target.status_code == 201, target.text

    async with app.state.database.sessions() as session:
        action = await session.scalar(select(PlatformActionObservation))
        target_observation = await session.get(
            EventObservation,
            target.json()["observation_id"],
        )
        assert action is not None
        assert action.resolver_status == "resolved"
        assert action.target_source_event_id == target.json()["source_event_id"]
        assert target_observation is not None
        assert target_observation.capture_profile == "operational"
        assert target_observation.capture_policy_version == "group-policy-v2"


async def test_reused_short_message_id_with_distinct_v2_identity_creates_two_sources(
    client,
    app,
) -> None:
    occurred_at = datetime.now(timezone.utc)
    first_payload = event_payload(
        source_event_id=f"qq:source:v2:{'a' * 64}",
        message_id="collision-prone-short-id",
        real_seq="native-seq-100",
        occurred_at=occurred_at,
    )
    second_payload = event_payload(
        source_event_id=f"qq:source:v2:{'b' * 64}",
        message_id="collision-prone-short-id",
        real_seq="native-seq-101",
        occurred_at=occurred_at,
    )

    first = await client.post(
        "/v1/events",
        json=first_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "short-collision-a",
        },
    )
    second = await client.post(
        "/v1/events",
        json=second_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "short-collision-b",
        },
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["source_event_id"] != second.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2
        observations = (await session.scalars(select(EventObservation))).all()
        assert len(observations) == 2
        assert {item.platform_message_id for item in observations} == {"collision-prone-short-id"}


async def test_reused_event_identity_with_changed_native_identity_is_rejected(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    payload = event_payload(
        source_event_id=f"qq:source:v2:{'c' * 64}",
        message_id="reused-short-id",
        real_seq="native-seq-original",
        occurred_at=occurred_at,
    )
    headers = {
        "Authorization": "Bearer lily-secret",
        "Idempotency-Key": "reused-event-identity",
    }
    created = await client.post("/v1/events", json=payload, headers=headers)
    conflicting = event_payload(
        source_event_id=payload["source_event_id"],
        message_id="reused-short-id",
        real_seq="native-seq-different",
        occurred_at=occurred_at,
    )
    rejected = await client.post("/v1/events", json=conflicting, headers=headers)

    assert created.status_code == 201
    assert rejected.status_code == 409
    assert "reused for a different event" in rejected.json()["detail"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 1
        assert len((await session.scalars(select(EventObservation))).all()) == 1


async def test_nul_in_event_and_response_is_stored_safely(client, app) -> None:
    event = event_payload(
        text="前\x00后",
        message_id="nul-event",
        source_event_id="qq:group:123:message:nul-event",
    )
    event["conversation"]["name"] = "群\x00名"
    event["sender"]["name"] = "发\x00送者"
    event["metadata"]["nested"] = {"key": "值\x00尾"}
    created = await client.post(
        "/v1/events",
        json=event,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "nul-event"},
    )
    assert created.status_code == 201, created.text

    response_payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:985393579:message:nul-response",
        "instance": event["instance"],
        "trigger_source_event_id": event["source_event_id"],
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "nul-response",
        "text": "答\x00案",
        "segments": [{"type": "text", "data": {"text": "答\x00案"}}],
        "attachments": [],
        "success": False,
        "error": "错\x00误",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {"nested": {"key": "值\x00尾"}},
    }
    response = await client.post(
        "/v1/responses",
        json=response_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "nul-response",
        },
    )
    assert response.status_code == 201, response.text

    async with app.state.database.sessions() as session:
        observation = await session.scalar(select(EventObservation))
        stored_response = await session.scalar(select(ResponseRecord))
        assert observation is not None
        assert stored_response is not None
        assert observation.text == "前\ufffd后"
        assert observation.conversation_name == "群\ufffd名"
        assert observation.sender_name == "发\ufffd送者"
        assert observation.segments_json[0]["data"]["text"] == "前\ufffd后"
        assert observation.metadata_json["nested"]["key"] == "值\ufffd尾"
        assert stored_response.text == "答\ufffd案"
        assert stored_response.error == "错\ufffd误"
        assert stored_response.segments_json[0]["data"]["text"] == "答\ufffd案"
        assert stored_response.metadata_json["nested"]["key"] == "值\ufffd尾"


async def test_sender_title_and_level_are_stored_as_event_time_facts(client, app) -> None:
    event = event_payload(
        source_event_id="qq:group:123:message:sender-profile",
        message_id="sender-profile",
    )
    event["sender"]["title"] = "群之龙王"
    event["sender"]["level"] = "活跃等级 42"
    created = await client.post(
        "/v1/events",
        json=event,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "sender-profile",
        },
    )
    assert created.status_code == 201, created.text

    async with app.state.database.sessions() as session:
        observation = await session.get(
            EventObservation,
            created.json()["observation_id"],
        )

    assert observation is not None
    assert observation.sender_title == "群之龙王"
    assert observation.sender_level == "活跃等级 42"


async def test_native_identity_is_visible_in_admin_debug_views(client) -> None:
    payload = event_payload()
    payload["metadata"] = {
        "native_identity": {
            "schema": "onebot_v11.qq.native_identity.v1",
            "message_id": "456",
            "real_seq": "778899",
            "group_id": "123",
        }
    }
    event = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "native-identity-event",
        },
    )
    assert event.status_code == 201

    assert (await client.get("/v1/native-identities/recent")).status_code == 401
    headers = {"Authorization": "Bearer admin-secret"}
    recent = await client.get("/v1/native-identities/recent", headers=headers)
    events = await client.get("/v1/events/recent", headers=headers)
    context = await client.get(f"/v1/events/{event.json()['source_event_id']}/context", headers=headers)
    coverage = await client.get("/v1/native-identities/coverage?hours=1", headers=headers)

    assert recent.status_code == events.status_code == context.status_code == coverage.status_code == 200
    assert recent.json()[0]["native_identity"]["real_seq"] == "778899"
    assert "real_seq=778899" in recent.json()[0]["summary"]
    assert events.json()[0]["native_identity"]["real_seq"] == "778899"
    assert context.json()["observations"][0]["native_identity"]["real_seq"] == "778899"
    assert coverage.json()["instances"] == [
        {
            "instance_id": "lily-command",
            "observations": 1,
            "with_native_identity": 1,
            "fields": {"message_id": 1, "real_seq": 1, "group_id": 1},
            "coverage_percent": 100.0,
        }
    ]


async def test_two_bots_create_two_observations_of_one_source_event(client) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily = await client.post(
        "/v1/events",
        json=event_payload("lily-command", occurred_at=occurred_at),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "lily-observation",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=event_payload("nekro-agent", occurred_at=occurred_at),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "nekro-observation",
        },
    )
    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    assert lily.json()["observation_id"] != nekro.json()["observation_id"]


async def test_correlated_event_gets_one_shadow_decision(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily = await client.post(
        "/v1/events",
        json=event_payload("lily-command", occurred_at=occurred_at),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:nekro-shadow",
            conversation_id="group_123",
            message_id="456",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "decision-nekro",
        },
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
    occurred_at = datetime.now(timezone.utc)
    lily_request = client.post(
        "/v1/events",
        json=event_payload("lily-command", occurred_at=occurred_at),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "concurrent-lily",
        },
    )
    nekro_request = client.post(
        "/v1/events",
        json=event_payload("nekro-agent", occurred_at=occurred_at),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "concurrent-nekro",
        },
    )
    lily, nekro = await asyncio.gather(lily_request, nekro_request)
    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]


async def test_command_event_records_shadow_decision_for_lily(client, app) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload(
            text="wf 1+1",
            message_id="command",
            source_event_id="qq:group:123:message:command",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-command",
        },
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.source_event_id == response.json()["source_event_id"]
        assert decision.policy_version == "qq-v3-policy-v6"
        assert decision.decision_type == "command"
        assert decision.target_instance_id == "lily-command"
        assert decision.confidence == 95
        assert decision.reason.startswith("command_prefix:wf")
        assert decision.features_json["command_prefix"] == "wf"
        assert decision.features_json["matched_command"]["rule_id"] == "lily.wolfram"
        assert decision.features_json["matched_command"]["source_plugin"] == "plugins.wolfram"


@pytest.mark.parametrize(
    ("leading_segment", "case_id"),
    [
        ({"type": "at", "data": {"qq": "111222333"}}, "at-other"),
        ({"type": "image", "data": {"file": "opaque-image-id"}}, "image-first"),
    ],
)
async def test_non_text_or_other_at_leading_segment_cannot_become_lily_command(
    client,
    app,
    leading_segment: dict,
    case_id: str,
) -> None:
    payload = event_payload(
        source_event_id=f"qq:group:123:message:ineligible-{case_id}",
        message_id=f"ineligible-{case_id}",
        text="今日老婆",
    )
    payload["message"]["segments"] = [
        leading_segment,
        {"type": "text", "data": {"text": "今日老婆"}},
    ]
    created = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": f"ineligible-{case_id}",
        },
    )

    assert created.status_code == 201
    async with app.state.database.sessions() as session:
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == created.json()["source_event_id"])
        )
        assert decision is not None
        assert decision.decision_type == "observe_only"
        assert decision.features_json["command_eligible"] is False
        assert decision.features_json["matched_command"] is None


async def test_leading_own_at_with_to_me_remains_command_eligible(client, app) -> None:
    payload = event_payload(
        source_event_id="qq:group:123:message:eligible-own-at",
        message_id="eligible-own-at",
        text="今日老婆",
    )
    payload["metadata"]["to_me"] = True
    payload["message"]["segments"] = [
        {"type": "at", "data": {"qq": payload["instance"]["bot_id"]}},
        {"type": "text", "data": {"text": "今日老婆"}},
    ]
    created = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "eligible-own-at",
        },
    )

    assert created.status_code == 201
    async with app.state.database.sessions() as session:
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == created.json()["source_event_id"])
        )
        assert decision is not None
        assert decision.decision_type == "command"
        assert decision.target_instance_id == "lily-command"
        assert decision.features_json["command_eligible"] is True
        assert decision.features_json["matched_command"]["rule_id"] == "external.today_waifu.public"


async def test_to_me_event_records_shadow_decision_for_nekro_and_debug_views(
    client,
) -> None:
    payload = event_payload(
        text="莉莉帮我看看",
        message_id="to-me",
        source_event_id="qq:group:123:message:to-me",
    )
    payload["metadata"] = {"to_me": True}
    response = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-to-me",
        },
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


async def test_canonical_group_mode_key_controls_conversation_routing(client, app) -> None:
    app.state.settings = replace(
        app.state.settings,
        group_default_mode="command_only",
        group_modes={"qq:group:123": "full"},
    )
    full = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:mode-full",
            message_id="mode-full",
            text="莉莉，full",
        ),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "mode-full"},
    )
    restricted = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:456:message:mode-command-only",
            conversation_id="456",
            message_id="mode-command-only",
            text="莉莉，command-only",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "mode-command-only",
        },
    )
    assert full.status_code == restricted.status_code == 201

    async with app.state.database.sessions() as session:
        full_decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == full.json()["source_event_id"])
        )
        restricted_decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == restricted.json()["source_event_id"])
        )
        assert full_decision is not None
        assert restricted_decision is not None
        assert full_decision.decision_type == "talk"
        assert full_decision.features_json["conversation_mode"] == "full"
        assert restricted_decision.decision_type == "observe_only"
        assert restricted_decision.reason == "conversation_mode_command_only"
        assert restricted_decision.features_json["conversation_mode"] == "command_only"


async def test_conversation_only_group_suppresses_lily_commands_but_routes_nekro(client, app) -> None:
    app.state.settings = replace(
        app.state.settings,
        group_modes={"qq:group:123": "conversation_only"},
    )
    command = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:conversation-only-command",
            message_id="conversation-only-command",
            text="换老婆",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "conversation-only-command",
        },
    )
    summon = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:conversation-only-summon",
            message_id="conversation-only-summon",
            text="莉莉在吗",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "conversation-only-summon",
        },
    )
    assert command.status_code == summon.status_code == 201

    async with app.state.database.sessions() as session:
        command_decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == command.json()["source_event_id"])
        )
        summon_decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == summon.json()["source_event_id"])
        )
        assert command_decision is not None
        assert summon_decision is not None
        assert command_decision.decision_type == "observe_only"
        assert command_decision.reason == "command_target_unavailable"
        assert summon_decision.decision_type == "talk"
        assert summon_decision.target_instance_id == "nekro-agent"


async def test_command_registry_debug_endpoint_is_admin_only(client) -> None:
    denied = await client.get("/v1/command-registry")
    allowed = await client.get("/v1/command-registry", headers={"Authorization": "Bearer admin-secret"})

    assert denied.status_code == 401
    assert allowed.status_code == 200
    payload = allowed.json()
    assert payload["version"] == "2026-07-16-phase2-policy-v5.1"
    assert any(rule["id"] == "lily.wolfram" for rule in payload["rules"])
    assert any(
        rule["id"] == "external.random.draw" and rule["runtime_introspection"] == "reviewed"
        for rule in payload["rules"]
    )
    assert any(rule["id"] == "external.updater.control" and rule["sensitive"] for rule in payload["rules"])


async def test_runtime_command_registry_snapshot_is_authenticated_and_auditable(
    client,
) -> None:
    payload = {
        "schema_version": "1.0",
        "instance": event_payload()["instance"],
        "snapshot_hash": "a" * 64,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "plugins": [
            {
                "plugin_id": "wolfram",
                "module_name": "plugins.wolfram",
                "display_name": "Wolfram",
                "matcher_count": 1,
                "classified_matcher_count": 1,
            }
        ],
        "candidates": [
            {
                "plugin_id": "wolfram",
                "module_name": "plugins.wolfram",
                "matcher_type": "message",
                "kind": "command",
                "triggers": ["wf", "wolfram-extra"],
                "priority": 10,
                "block": True,
                "ignore_case": None,
                "regex_flags": None,
                "complete": True,
                "rule_checker_count": 1,
                "unknown_rule_checkers": [],
                "permission_checker_count": 0,
            }
        ],
    }
    payload["snapshot_hash"] = runtime_registry_snapshot_hash(payload["plugins"], payload["candidates"])
    denied_write = await client.post(
        "/v1/command-registry/snapshots",
        json=payload,
        headers={"Authorization": "Bearer nekro-secret"},
    )
    assert denied_write.status_code == 403
    nekro_snapshot = {**payload, "instance": event_payload("nekro-agent")["instance"]}
    denied_nekro_registry = await client.post(
        "/v1/command-registry/snapshots",
        json=nekro_snapshot,
        headers={"Authorization": "Bearer nekro-secret"},
    )
    assert denied_nekro_registry.status_code == 403
    bad_hash = await client.post(
        "/v1/command-registry/snapshots",
        json={**payload, "snapshot_hash": "c" * 64},
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert bad_hash.status_code == 422
    first = await client.post(
        "/v1/command-registry/snapshots",
        json=payload,
        headers={"Authorization": "Bearer lily-secret"},
    )
    refreshed_payload = {
        **payload,
        "observed_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }
    second = await client.post(
        "/v1/command-registry/snapshots",
        json=refreshed_payload,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["duplicate"] is True

    assert (await client.get("/v1/command-registry/runtime")).status_code == 401
    runtime = await client.get(
        "/v1/command-registry/runtime",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert runtime.status_code == 200
    body = runtime.json()
    assert body["summary"]["snapshot_instances"] == 1
    returned_observed_at = datetime.fromisoformat(body["snapshots"][0]["observed_at"])
    if returned_observed_at.tzinfo is None:
        returned_observed_at = returned_observed_at.replace(tzinfo=timezone.utc)
    assert returned_observed_at == datetime.fromisoformat(refreshed_payload["observed_at"])
    assert body["summary"]["runtime_candidates"] == 1
    assert body["summary"]["uncovered_candidate_triggers"] == 1
    assert body["uncovered_candidates"][0]["uncovered_triggers"] == ["wolfram-extra"]
    wolfram = next(item for item in body["static_rules"] if item["id"] == "lily.wolfram")
    assert wolfram["runtime_loaded"] is True

    unregistered = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:runtime-unregistered",
            message_id="runtime-unregistered",
            text="wolfram-extra 1+1",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "runtime-unregistered",
        },
    )
    assert unregistered.status_code == 201
    decisions = await client.get(
        "/v1/decisions/recent?limit=1",
        headers={"Authorization": "Bearer admin-secret"},
    )
    runtime_features = decisions.json()[0]["features"]["command_registry_runtime"]
    assert runtime_features["status"] == "fresh"
    assert runtime_features["unregistered_match"]["trigger"] == "wolfram-extra"


async def test_reply_to_lily_response_records_observe_only_decision(client, app) -> None:
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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-bot-parent",
        },
    )
    assert bot_response.status_code == 201

    event = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:reply-to-bot",
            message_id="reply-to-bot",
            text="莉莉换老婆",
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "bot-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-reply-to-bot",
        },
    )
    assert event.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.decision_type == "observe_only"
        assert decision.target_instance_id is None
        assert decision.reason == "reply_to_command_response_observed"
        assert decision.features_json["reply_to_bot_response"] is True
        assert decision.features_json["reply_target_instance_id"] == "lily-command"


async def test_reply_to_nekro_response_records_talk_decision(client, app) -> None:
    response_payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:2022692714:message:nekro-parent",
        "instance": event_payload("nekro-agent")["instance"],
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "nekro-parent",
        "text": "nekro says hi",
        "segments": [],
        "attachments": [],
        "success": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    bot_response = await client.post(
        "/v1/responses",
        json=response_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "decision-nekro-parent",
        },
    )
    assert bot_response.status_code == 201

    event = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:123:message:reply-to-nekro",
            message_id="reply-to-nekro",
            text="换老婆",
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "nekro-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "decision-reply-to-nekro",
        },
    )
    assert event.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.decision_type == "talk"
        assert decision.target_instance_id == "nekro-agent"
        assert decision.reason == "reply_to_talk_response"
        assert decision.features_json["reply_target_instance_id"] == "nekro-agent"


@pytest.mark.parametrize("target_instance_id", ["lily-command", "nekro-agent"])
@pytest.mark.parametrize("keep_auto_at", [False, True])
@pytest.mark.parametrize("arrival_order", [("lily-command", "nekro-agent"), ("nekro-agent", "lily-command")])
async def test_canonical_reply_decision_is_independent_of_auto_at_and_arrival_order(
    client,
    app,
    target_instance_id: str,
    keep_auto_at: bool,
    arrival_order: tuple[str, str],
) -> None:
    target_payload = event_payload(target_instance_id)
    target_bot_id = target_payload["instance"]["bot_id"]
    target_message_id = f"parent-{target_instance_id}-{keep_auto_at}-{arrival_order[0]}"
    target_token = "lily-secret" if target_instance_id == "lily-command" else "nekro-secret"
    response = await client.post(
        "/v1/responses",
        json={
            "schema_version": "1.0",
            "source_response_id": f"qq:{target_bot_id}:message:{target_message_id}",
            "instance": target_payload["instance"],
            "response_type": "message",
            "conversation": {"id": "123", "type": "group"},
            "platform_message_id": target_message_id,
            "text": "parent",
            "segments": [],
            "attachments": [],
            "success": True,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
        headers={
            "Authorization": f"Bearer {target_token}",
            "Idempotency-Key": f"response-{target_message_id}",
        },
    )
    assert response.status_code == 201

    child_responses = []
    child_occurred_at = datetime.now(timezone.utc)
    for instance_id in arrival_order:
        local_parent_id = target_message_id if instance_id == target_instance_id else f"other-view-{target_message_id}"
        payload = event_payload(
            instance_id,
            source_event_id=f"qq:group:123:message:child-{instance_id}-{target_message_id}",
            message_id=f"child-{instance_id}-{target_message_id}",
            real_seq=f"child-seq-{target_message_id}",
            text="继续",
            occurred_at=child_occurred_at,
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": local_parent_id,
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        )
        if keep_auto_at:
            payload["message"]["segments"] = [
                {"type": "at", "data": {"qq": target_bot_id}},
                {"type": "text", "data": {"text": "继续"}},
            ]
        token = "lily-secret" if instance_id == "lily-command" else "nekro-secret"
        child_responses.append(
            await client.post(
                "/v1/events",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": f"child-{instance_id}-{target_message_id}",
                },
            )
        )

    assert all(item.status_code == 201 for item in child_responses)
    assert child_responses[0].json()["source_event_id"] == child_responses[1].json()["source_event_id"]
    async with app.state.database.sessions() as session:
        decisions = (await session.scalars(select(EventDecision))).all()
        assert len(decisions) == 1
        decision = decisions[0]
        assert decision.revision == 2
        assert decision.features_json["observation_count"] == 2
        assert decision.features_json["mentioned_bot_instance_ids"] == []
        assert decision.features_json["reply_target_instance_id"] == target_instance_id
        if target_instance_id == "nekro-agent":
            assert decision.decision_type == "talk"
            assert decision.target_instance_id == "nekro-agent"
            assert decision.reason == "reply_to_talk_response"
        else:
            assert decision.decision_type == "observe_only"
            assert decision.target_instance_id is None
            assert decision.reason == "reply_to_command_response_observed"


async def test_ambiguous_reply_link_cannot_be_upgraded_by_response_fallback(client, app) -> None:
    child_time = datetime.now(timezone.utc)
    for suffix, seconds, real_seq in (
        ("a", -3, "ambiguous-parent-a"),
        ("b", -2, "ambiguous-parent-b"),
    ):
        parent = await client.post(
            "/v1/events",
            json=event_payload(
                source_event_id=f"qq:source:v2:ambiguous-parent-{suffix}",
                message_id="ambiguous-short-parent",
                real_seq=real_seq,
                occurred_at=child_time + timedelta(seconds=seconds),
            ),
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"ambiguous-parent-{suffix}",
            },
        )
        assert parent.status_code == 201

    bot_response = await client.post(
        "/v1/responses",
        json=response_payload(
            source_response_id="qq:985393579:message:ambiguous-short-parent",
            platform_message_id="ambiguous-short-parent",
            occurred_at=child_time - timedelta(seconds=1),
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "ambiguous-response",
        },
    )
    assert bot_response.status_code == 201

    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:ambiguous-child",
            message_id="ambiguous-child",
            real_seq="ambiguous-child",
            text="继续",
            occurred_at=child_time,
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "ambiguous-short-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "ambiguous-child",
        },
    )
    assert child.status_code == 201

    async with app.state.database.sessions() as session:
        link = await session.scalar(
            select(EventLink).where(EventLink.from_source_event_id == child.json()["source_event_id"])
        )
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == child.json()["source_event_id"])
        )
        assert link is not None and link.resolver_status == "ambiguous"
        assert decision is not None
        assert decision.decision_type == "observe_only"
        assert decision.reason == "reply_target_conflict_observed"
        assert decision.features_json["reply_target_status"] == "ambiguous"
        assert decision.features_json["reply_target_instance_id"] is None


async def test_resolved_human_reply_and_bot_response_fallback_is_conflict(client, app) -> None:
    child_time = datetime.now(timezone.utc)
    parent = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:human-parent",
            message_id="shared-human-bot-short-id",
            real_seq="human-parent",
            text="human parent",
            occurred_at=child_time - timedelta(seconds=2),
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "human-parent",
        },
    )
    response = await client.post(
        "/v1/responses",
        json=response_payload(
            source_response_id="qq:985393579:message:shared-human-bot-short-id",
            platform_message_id="shared-human-bot-short-id",
            occurred_at=child_time - timedelta(seconds=1),
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "shared-bot-response",
        },
    )
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:human-bot-conflict-child",
            message_id="human-bot-conflict-child",
            real_seq="human-bot-conflict-child",
            text="继续",
            occurred_at=child_time,
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "shared-human-bot-short-id",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "human-bot-conflict-child",
        },
    )
    assert parent.status_code == response.status_code == child.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == child.json()["source_event_id"])
        )
        assert decision is not None
        assert decision.decision_type == "observe_only"
        assert decision.reason == "reply_target_conflict_observed"
        assert decision.features_json["reply_target_status"] == "conflict"
        assert decision.features_json["reply_target_instance_id"] is None


async def test_future_response_cannot_retroactively_upgrade_earlier_reply(client, app) -> None:
    child_time = datetime.now(timezone.utc)
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:future-response-child",
            message_id="future-response-child",
            real_seq="future-response-child",
            text="继续",
            occurred_at=child_time,
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "future-response-short-id",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "future-response-child",
        },
    )
    assert child.status_code == 201
    async with app.state.database.sessions() as session:
        before = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == child.json()["source_event_id"])
        )
        assert before is not None
        assert before.reason == "reply_reference_observed"
        before_revision = before.revision

    future_response = await client.post(
        "/v1/responses",
        json=response_payload(
            source_response_id="qq:985393579:message:future-response-short-id",
            platform_message_id="future-response-short-id",
            occurred_at=child_time + timedelta(seconds=5),
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "future-response",
        },
    )
    assert future_response.status_code == 201

    async with app.state.database.sessions() as session:
        after = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == child.json()["source_event_id"])
        )
        assert after is not None
        assert after.revision == before_revision
        assert after.reason == "reply_reference_observed"
        assert after.features_json["reply_target_status"] == "unresolved"


async def test_non_message_event_records_ignore_shadow_decision(client, app) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload(
            text=None,
            message_id="notice",
            source_event_id="qq:group:123:notice:recall",
            event_type="notice.group_recall",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "decision-notice",
        },
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision))
        assert decision is not None
        assert decision.decision_type == "ignore"
        assert decision.target_instance_id is None
        assert decision.reason == "non_message_event"


async def test_cross_account_different_message_ids_merge_on_real_seq(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:lily-456",
        message_id="lily-456",
        real_seq="canonical-7788",
        occurred_at=occurred_at,
        event_type="message.group.normal",
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:group_123:message:nekro-789",
        conversation_id="group_123",
        message_id="nekro-789",
        real_seq="canonical-7788",
        occurred_at=occurred_at,
    )

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "local-id-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "local-id-nekro",
        },
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        sources = (await session.scalars(select(SourceEvent))).all()
        observations = (await session.scalars(select(EventObservation))).all()
        assert len(sources) == 1
        assert sources[0].correlation_version == "qq-message-v3"
        assert {item.conversation_id for item in sources} == {"123"}
        assert {item.event_type for item in sources} == {"message"}
        assert {item.reported_source_event_id for item in observations} == {
            lily_payload["source_event_id"],
            nekro_payload["source_event_id"],
        }
        assert {item.platform_message_id for item in observations} == {
            "lily-456",
            "nekro-789",
        }


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
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"repeat-{suffix}",
            },
        )
        assert response.status_code == 201

    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_cross_bot_strong_identity_merges_despite_normalization_delay(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily = await client.post(
        "/v1/events",
        json=event_payload(
            "lily-command",
            source_event_id="qq:group:123:message:early",
            message_id="early",
            real_seq="same-native-seq",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "outside-lily",
        },
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:group_123:message:late",
        conversation_id="group_123",
        message_id="late",
        real_seq="same-native-seq",
        occurred_at=occurred_at + timedelta(seconds=6),
    )
    # Both accounts preserve the platform time and real_seq.  Nekro's
    # normalized ChatMessage can be several seconds later than Lily's raw
    # OneBot event, but that delivery delay must not split one QQ message.
    nekro_payload["metadata"]["native_identity"]["time"] = str(int(occurred_at.timestamp()))
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "outside-nekro",
        },
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 1


async def test_native_time_conflict_keeps_strong_identity_events_separate(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:time-conflict-lily",
        message_id="time-conflict-lily",
        real_seq="time-conflict-seq",
        occurred_at=occurred_at,
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:group_123:message:time-conflict-nekro",
        conversation_id="group_123",
        message_id="time-conflict-nekro",
        real_seq="time-conflict-seq",
        occurred_at=occurred_at,
    )
    nekro_payload["metadata"]["native_identity"]["time"] = str(int(occurred_at.timestamp()) + 1)

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "time-conflict-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "time-conflict-nekro",
        },
    )
    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] != nekro.json()["source_event_id"]

    async with app.state.database.sessions() as session:
        observations = (await session.scalars(select(EventObservation))).all()
        statuses = {item.instance_id: item.metadata_json["correlation"]["status"] for item in observations}
        assert statuses == {
            "lily-command": "new_strong_identity",
            "nekro-agent": "native_time_conflict",
        }


async def test_missing_native_time_never_enables_unbounded_cross_account_merge(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_payload = event_payload(
        "lily-command",
        source_event_id=f"qq:source:v2:{'d' * 64}",
        message_id="missing-time-lily",
        real_seq="eventually-reused-real-seq",
        occurred_at=occurred_at,
    )
    lily_payload["metadata"]["native_identity"].pop("time")
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id=f"qq:source:v2:{'e' * 64}",
        message_id="missing-time-nekro",
        real_seq="eventually-reused-real-seq",
        occurred_at=occurred_at + timedelta(seconds=6),
    )

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "missing-time-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "missing-time-nekro",
        },
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] != nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_missing_native_time_uses_bounded_cross_account_fallback(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    lily_payload = event_payload(
        "lily-command",
        source_event_id=f"qq:source:v2:{'f' * 64}",
        message_id="bounded-time-lily",
        real_seq="bounded-real-seq",
        occurred_at=occurred_at,
    )
    lily_payload["metadata"]["native_identity"].pop("time")
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id=f"qq:source:v2:{'0' * 64}",
        message_id="bounded-time-nekro",
        real_seq="bounded-real-seq",
        occurred_at=occurred_at + timedelta(seconds=1),
    )

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "bounded-time-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "bounded-time-nekro",
        },
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]


async def test_partial_candidate_native_time_cannot_hide_known_conflict(client, app) -> None:
    occurred_at = datetime.now(timezone.utc).replace(microsecond=0)
    app.state.settings = replace(
        app.state.settings,
        ingest_tokens={
            **app.state.settings.ingest_tokens,
            "qq-standby": "standby-secret",
        },
    )
    lily_payload = event_payload(
        "lily-command",
        source_event_id=f"qq:source:v2:{'1' * 64}",
        message_id="partial-time-lily",
        real_seq="partial-time-seq",
        occurred_at=occurred_at,
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id=f"qq:source:v2:{'2' * 64}",
        message_id="partial-time-nekro",
        real_seq="partial-time-seq",
        occurred_at=occurred_at + timedelta(seconds=1),
    )
    nekro_payload["metadata"]["native_identity"].pop("time")
    standby_payload = event_payload(
        "qq-standby",
        source_event_id=f"qq:source:v2:{'3' * 64}",
        message_id="partial-time-standby",
        real_seq="partial-time-seq",
        occurred_at=occurred_at + timedelta(seconds=1),
    )
    standby_payload["metadata"]["native_identity"]["time"] = str(int(occurred_at.timestamp()) + 1)

    lily = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "partial-time-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "partial-time-nekro",
        },
    )
    standby = await client.post(
        "/v1/events",
        json=standby_payload,
        headers={
            "Authorization": "Bearer standby-secret",
            "Idempotency-Key": "partial-time-standby",
        },
    )

    assert lily.status_code == nekro.status_code == standby.status_code == 201
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    assert standby.json()["source_event_id"] != lily.json()["source_event_id"]


async def test_same_instance_missing_native_time_cannot_alias_reused_identity(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    source_ids: list[str] = []
    for suffix, offset in (("old", 0), ("new", 6)):
        payload = event_payload(
            source_event_id=f"qq:source:v2:{suffix * 32}",
            message_id="reused-message-id",
            real_seq="reused-real-seq",
            occurred_at=occurred_at + timedelta(seconds=offset),
        )
        payload["metadata"]["native_identity"].pop("time")
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"missing-replay-{suffix}",
            },
        )
        assert response.status_code == 201, response.text
        source_ids.append(response.json()["source_event_id"])

    assert len(set(source_ids)) == 2


async def test_non_text_messages_merge_on_real_seq(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    responses = []
    for instance_id, suffix in (
        ("lily-command", "image-a"),
        ("nekro-agent", "image-b"),
    ):
        responses.append(
            await client.post(
                "/v1/events",
                json=event_payload(
                    instance_id,
                    source_event_id=f"qq:group:123:message:{suffix}",
                    message_id=suffix,
                    real_seq="shared-image-seq",
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
    assert responses[0].json()["source_event_id"] == responses[1].json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 1


async def test_missing_real_seq_never_falls_back_to_text_correlation(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    responses = []
    for instance_id, suffix in (
        ("lily-command", "missing-a"),
        ("nekro-agent", "missing-b"),
    ):
        payload = event_payload(
            instance_id,
            source_event_id=f"qq:group:123:message:{suffix}",
            message_id=suffix,
            text="草",
            occurred_at=occurred_at,
        )
        payload["metadata"] = {}
        responses.append(
            await client.post(
                "/v1/events",
                json=payload,
                headers={
                    "Authorization": f"Bearer {'lily-secret' if instance_id == 'lily-command' else 'nekro-secret'}",
                    "Idempotency-Key": f"missing-seq-{suffix}",
                },
            )
        )

    assert all(response.status_code == 201 for response in responses)
    assert responses[0].json()["source_event_id"] != responses[1].json()["source_event_id"]
    async with app.state.database.sessions() as session:
        assert len((await session.scalars(select(SourceEvent))).all()) == 2


async def test_private_messages_do_not_use_group_validated_cross_account_identity(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    payloads = []
    for instance_id, suffix in (("lily-command", "lily"), ("nekro-agent", "nekro")):
        payload = event_payload(
            instance_id,
            source_event_id=f"qq:private:sender:message:{suffix}",
            conversation_id="sender",
            message_id=f"{suffix}-private",
            real_seq="same-private-seq",
            text="same text",
            occurred_at=occurred_at,
        )
        payload["conversation"]["type"] = "private"
        payloads.append(payload)

    lily = await client.post(
        "/v1/events",
        json=payloads[0],
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "private-lily",
        },
    )
    nekro = await client.post(
        "/v1/events",
        json=payloads[1],
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "private-nekro",
        },
    )

    assert lily.status_code == nekro.status_code == 201
    assert lily.json()["source_event_id"] != nekro.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        sources = (await session.scalars(select(SourceEvent))).all()
        assert len(sources) == 2
        assert all(item.correlation_version is None for item in sources)


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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "reference-parent",
        },
    )
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:child",
            message_id="child",
            text="reply message",
            occurred_at=occurred_at,
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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "reference-child",
        },
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
            real_seq="shared-parent-seq",
            text="shared parent",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "reference-cross-lily",
        },
    )
    nekro_parent = await client.post(
        "/v1/events",
        json=event_payload(
            "nekro-agent",
            source_event_id="qq:group:group_123:message:nekro-parent",
            conversation_id="group_123",
            message_id="nekro-parent-local",
            real_seq="shared-parent-seq",
            text="shared parent",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "reference-cross-nekro",
        },
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
                    "platform_message_id": "nekro-parent-local",
                    "conversation_id": "group_123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "reference-cross-child",
        },
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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "reference-unresolved",
        },
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


async def test_direct_canonical_reference_cannot_cross_conversation(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    parent = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:other:message:parent",
            conversation_id="other",
            message_id="parent",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "cross-conversation-parent",
        },
    )
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:cross-conversation-child",
            message_id="cross-conversation-child",
            occurred_at=occurred_at + timedelta(seconds=1),
            references=[
                {
                    "type": "reply_to",
                    "source_event_id": parent.json()["source_event_id"],
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "cross-conversation-child",
        },
    )

    assert parent.status_code == child.status_code == 201
    async with app.state.database.sessions() as session:
        link = await session.scalar(
            select(EventLink).where(EventLink.from_source_event_id == child.json()["source_event_id"])
        )
        assert link is not None
        assert link.to_source_event_id is None
        assert link.resolver_status == "unresolved"


async def test_late_parent_observation_backfills_unresolved_link(client, app) -> None:
    occurred_at = datetime.now(timezone.utc)
    child = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:late-child",
            message_id="late-child",
            occurred_at=occurred_at + timedelta(seconds=1),
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": "late-parent",
                    "conversation_id": "123",
                    "conversation_type": "group",
                }
            ],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "late-child",
        },
    )
    assert child.status_code == 201

    parent = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:late-parent",
            message_id="late-parent",
            occurred_at=occurred_at,
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "late-parent",
        },
    )
    assert parent.status_code == 201

    assert (await client.post("/v1/event-links/resolve")).status_code == 401
    resolved = await client.post(
        "/v1/event-links/resolve?limit=50",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert resolved.status_code == 200

    async with app.state.database.sessions() as session:
        link = await session.scalar(select(EventLink))
        assert link is not None
        assert link.resolver_status == "resolved"
        assert link.to_source_event_id == parent.json()["source_event_id"]
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == child.json()["source_event_id"])
        )
        assert decision is not None
        assert decision.reason == "reply_to_other_observed"
        assert decision.revision >= 2


async def test_response_trigger_resolves_reported_id_to_canonical_event(client, app) -> None:
    reported_source_id = "qq:group:123:message:account-local-trigger"
    event = await client.post(
        "/v1/events",
        json=event_payload(source_event_id=reported_source_id, message_id="account-local-trigger"),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "trigger-event",
        },
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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "trigger-response",
        },
    )
    assert response.status_code == 201

    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ResponseRecord))
        assert record is not None
        assert record.trigger_source_event_id == event.json()["source_event_id"]


async def test_response_trigger_cannot_use_source_seen_only_by_another_instance(
    client,
) -> None:
    event = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:cross-instance-trigger",
            message_id="cross-instance-trigger",
            real_seq="cross-instance-trigger",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "cross-instance-trigger",
        },
    )
    assert event.status_code == 201

    rejected = await client.post(
        "/v1/responses",
        json=response_payload(
            "nekro-agent",
            source_response_id="qq:2022692714:message:cross-instance-response",
            platform_message_id="cross-instance-response",
            trigger_source_event_id=event.json()["source_event_id"],
        ),
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "cross-instance-response",
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "trigger source was not observed by the response instance"


async def test_response_trigger_cannot_cross_conversations(client) -> None:
    event = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:source:v2:cross-conversation-trigger",
            message_id="cross-conversation-trigger",
            real_seq="cross-conversation-trigger",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "cross-conversation-trigger",
        },
    )
    assert event.status_code == 201

    rejected = await client.post(
        "/v1/responses",
        json=response_payload(
            source_response_id="qq:985393579:message:cross-conversation-response",
            platform_message_id="cross-conversation-response",
            conversation_id="999",
            trigger_source_event_id=event.json()["source_event_id"],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "cross-conversation-response",
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "response and trigger source belong to different conversations"


async def test_response_dual_trigger_hints_must_identify_the_same_event(client) -> None:
    events = []
    for suffix in ("observation", "source"):
        created = await client.post(
            "/v1/events",
            json=event_payload(
                source_event_id=f"qq:source:v2:dual-trigger-{suffix}",
                message_id=f"dual-trigger-{suffix}",
                real_seq=f"dual-trigger-{suffix}",
            ),
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": f"dual-trigger-{suffix}",
            },
        )
        assert created.status_code == 201
        events.append(created.json())

    rejected = await client.post(
        "/v1/responses",
        json=response_payload(
            source_response_id="qq:985393579:message:dual-trigger-response",
            platform_message_id="dual-trigger-response",
            trigger_observation_id=events[0]["observation_id"],
            trigger_source_event_id=events[1]["source_event_id"],
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "dual-trigger-response",
        },
    )

    assert rejected.status_code == 422
    assert rejected.json()["detail"] == "trigger observation and source identify different events"


async def test_late_event_backfills_an_early_response_trigger(client, app) -> None:
    reported_source_id = "qq:group:123:message:late-trigger-event"
    response_payload = {
        "schema_version": "1.0",
        "source_response_id": "qq:985393579:message:early-response",
        "instance": event_payload()["instance"],
        "trigger_source_event_id": reported_source_id,
        "response_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "platform_message_id": "early-response",
        "text": "early",
        "segments": [],
        "attachments": [],
        "success": True,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    response = await client.post(
        "/v1/responses",
        json=response_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "early-response",
        },
    )
    assert response.status_code == 201
    async with app.state.database.sessions() as session:
        unresolved = await session.scalar(select(ResponseRecord))
        assert unresolved is not None
        assert unresolved.trigger_source_event_id == reported_source_id
        assert unresolved.metadata_json["trigger_resolution_status"] == "unresolved"

    event = await client.post(
        "/v1/events",
        json=event_payload(source_event_id=reported_source_id, message_id="late-trigger-event"),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "late-trigger-event",
        },
    )

    assert event.status_code == 201
    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ResponseRecord))
        assert record is not None
        assert record.trigger_source_event_id == event.json()["source_event_id"]


async def test_decision_outcome_endpoint_compares_actual_response(client) -> None:
    command_reported_id = "qq:group:123:message:audit-command"
    command = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id=command_reported_id,
            message_id="audit-command",
            text="wf 1+1",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "audit-command",
        },
    )
    assert command.status_code == 201
    response = await client.post(
        "/v1/responses",
        json={
            "schema_version": "1.0",
            "source_response_id": "qq:985393579:message:audit-response",
            "instance": event_payload()["instance"],
            "trigger_source_event_id": command_reported_id,
            "response_type": "message",
            "conversation": {"id": "123", "type": "group"},
            "platform_message_id": "audit-response",
            "text": "2",
            "segments": [],
            "attachments": [],
            "success": True,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "audit-response",
        },
    )
    assert response.status_code == 201
    ordinary = await client.post(
        "/v1/events",
        json=event_payload(
            source_event_id="qq:group:123:message:audit-ordinary",
            message_id="audit-ordinary",
            text="普通消息",
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "audit-ordinary",
        },
    )
    assert ordinary.status_code == 201
    suppressed = await client.post(
        "/v1/responses",
        json={
            "schema_version": "1.0",
            "source_response_id": "qq:985393579:suppressed-attempt:audit",
            "instance": event_payload()["instance"],
            "trigger_source_event_id": ordinary.json()["source_event_id"],
            "response_type": "send_group_msg",
            "conversation": {"id": "123", "type": "group"},
            "text": "不会真正发送",
            "segments": [],
            "attachments": [],
            "success": False,
            "error": "blocked_by_core_claim",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {
                "completion_status": "suppressed",
                "trigger_attribution": "event_context",
            },
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "audit-suppressed-response",
        },
    )
    assert suppressed.status_code == 201

    assert (await client.get("/v1/decisions/outcomes")).status_code == 401
    audit = await client.get(
        "/v1/decisions/outcomes?hours=1&grace_seconds=0",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert audit.status_code == 200
    body = audit.json()
    assert body["decisions"] == 2
    assert body["outcomes"] == {"matched": 1, "matched_no_response": 1}
    assert body["responses"]["linked"] == 2
    assert body["responses"]["unlinked"] == 0


async def test_claim_canary_requires_peer_deny_before_enforcing_allow(client, app) -> None:
    app.state.settings = replace(
        app.state.settings,
        claim_mode="canary",
        claim_canary_conversations=frozenset({"qq:group:123"}),
        claim_coalesce_milliseconds=500,
    )
    observed_at = datetime.now(timezone.utc)
    snapshot = {
        "schema_version": "1.0",
        "instance": event_payload()["instance"],
        "snapshot_hash": "b" * 64,
        "observed_at": observed_at.isoformat(),
        "plugins": [
            {
                "plugin_id": "wolfram",
                "module_name": "plugins.wolfram",
                "display_name": "Wolfram",
                "matcher_count": 1,
                "classified_matcher_count": 1,
            }
        ],
        "candidates": [
            {
                "plugin_id": "wolfram",
                "module_name": "plugins.wolfram",
                "matcher_type": "message",
                "kind": "command",
                "triggers": ["wf"],
                "priority": 10,
                "block": True,
                "ignore_case": None,
                "regex_flags": None,
                "complete": True,
                "rule_checker_count": 1,
                "unknown_rule_checkers": [],
                "permission_checker_count": 0,
            }
        ],
    }
    snapshot["snapshot_hash"] = runtime_registry_snapshot_hash(snapshot["plugins"], snapshot["candidates"])
    snapshot_response = await client.post(
        "/v1/command-registry/snapshots",
        json=snapshot,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert snapshot_response.status_code == 201

    for instance_id, token in (
        ("lily-command", "lily-secret"),
        ("nekro-agent", "nekro-secret"),
    ):
        heartbeat = await client.post(
            "/v1/heartbeats",
            json={
                "schema_version": "1.0",
                "instance": event_payload(instance_id)["instance"],
                "process_status": "running",
                "connection_status": "connected",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert heartbeat.status_code == 200

    lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:claim-lily",
        message_id="claim-lily",
        text="wf 1+1",
        real_seq="991122",
        occurred_at=observed_at,
    )
    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:group_123:message:claim-nekro",
        conversation_id="group_123",
        message_id="claim-nekro",
        text="wf 1+1",
        real_seq="991122",
        occurred_at=observed_at,
    )
    lily_event = await client.post(
        "/v1/events",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "claim-event-lily",
        },
    )
    nekro_event = await client.post(
        "/v1/events",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "claim-event-nekro",
        },
    )
    assert lily_event.status_code == nekro_event.status_code == 201

    # The non-target must receive deny before Core may call the target an
    # exclusive owner.  This ordering models the safe coordination handshake.
    nekro = await client.post(
        "/v1/claims/evaluate",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "claim-command-nekro",
        },
    )
    nekro_ack = await client.post(
        f"/v1/claims/{nekro.json()['claim_id']}/ack",
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "claim-command-nekro-ack",
        },
    )
    assert nekro_ack.status_code == 200
    lily = await client.post(
        "/v1/claims/evaluate",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "claim-command-lily",
        },
    )

    assert lily.status_code == nekro.status_code == 200
    assert lily.json()["source_event_id"] == nekro.json()["source_event_id"]
    assert (lily.json()["action"], lily.json()["enforced"]) == ("allow", True)
    assert (nekro.json()["action"], nekro.json()["enforced"]) == ("deny", True)
    assert lily.json()["decision_revision"] == nekro.json()["decision_revision"] == 2
    assert lily.json()["features"]["coordination"] == {
        "observed_peer_instance_ids": ["nekro-agent"],
        "enforced_deny_instance_ids": ["nekro-agent"],
        "acknowledged_deny_instance_ids": ["nekro-agent"],
    }

    async with app.state.database.sessions() as session:
        claims = (await session.scalars(select(EventClaim))).all()
        assert len(claims) == 2
        assert sum(item.action == "allow" and item.enforced for item in claims) == 1

    assert (await client.get("/v1/claims/recent")).status_code == 401
    summary = await client.get(
        "/v1/claims/summary?hours=1",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert summary.status_code == 200
    assert summary.json()["actions"] == {"allow": 1, "deny": 1}
    assert summary.json()["enforced"] == {"allow": 1, "deny": 1}
    assert summary.json()["acknowledged"] == {"deny": 1}
    context = await client.get(
        f"/v1/events/{lily.json()['source_event_id']}/context",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert context.status_code == 200
    assert len(context.json()["claims"]) == 2

    concurrent_lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:claim-concurrent-lily",
        message_id="claim-concurrent-lily",
        text="wf 2+2",
        real_seq="991123",
        occurred_at=observed_at,
    )
    concurrent_nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:123:message:claim-concurrent-nekro",
        message_id="claim-concurrent-nekro",
        text="wf 2+2",
        real_seq="991123",
        occurred_at=observed_at,
    )
    for payload, token, key in (
        (concurrent_lily_payload, "lily-secret", "concurrent-event-lily"),
        (concurrent_nekro_payload, "nekro-secret", "concurrent-event-nekro"),
    ):
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )
        assert response.status_code == 201

    concurrent_lily, concurrent_nekro = await asyncio.gather(
        client.post(
            "/v1/claims/evaluate",
            json=concurrent_lily_payload,
            headers={
                "Authorization": "Bearer lily-secret",
                "Idempotency-Key": "concurrent-claim-lily",
            },
        ),
        client.post(
            "/v1/claims/evaluate",
            json=concurrent_nekro_payload,
            headers={
                "Authorization": "Bearer nekro-secret",
                "Idempotency-Key": "concurrent-claim-nekro",
            },
        ),
    )
    assert (concurrent_nekro.json()["action"], concurrent_nekro.json()["enforced"]) == (
        "deny",
        True,
    )
    assert (
        concurrent_lily.json()["action"],
        concurrent_lily.json()["reason"],
        concurrent_lily.json()["enforced"],
    ) == ("abstain", "claim_peer_suppressions_not_acknowledged", False)

    concurrent_source = concurrent_lily.json()["source_event_id"]
    async with app.state.database.sessions() as session:
        concurrent_claims = (
            await session.scalars(select(EventClaim).where(EventClaim.source_event_id == concurrent_source))
        ).all()
        enforced_allow = [item for item in concurrent_claims if item.action == "allow" and item.enforced]
        enforced_deny = [item for item in concurrent_claims if item.action == "deny" and item.enforced]
        assert len(enforced_deny) == 1
    assert not enforced_allow


async def test_late_target_cannot_claim_allow_after_peer_already_failed_open(client, app) -> None:
    app.state.settings = replace(
        app.state.settings,
        claim_mode="canary",
        claim_canary_conversations=frozenset({"qq:group:123"}),
        claim_coalesce_milliseconds=0,
    )
    observed_at = datetime.now(timezone.utc)

    snapshot = {
        "schema_version": "1.0",
        "instance": event_payload("lily-command")["instance"],
        "snapshot_hash": runtime_registry_snapshot_hash([], []),
        "observed_at": observed_at.isoformat(),
        "plugins": [],
        "candidates": [],
    }
    snapshot_response = await client.post(
        "/v1/command-registry/snapshots",
        json=snapshot,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert snapshot_response.status_code == 201

    for instance_id, token in (
        ("lily-command", "lily-secret"),
        ("nekro-agent", "nekro-secret"),
    ):
        heartbeat = await client.post(
            "/v1/heartbeats",
            json={
                "schema_version": "1.0",
                "instance": event_payload(instance_id)["instance"],
                "process_status": "running",
                "connection_status": "connected",
                "occurred_at": observed_at.isoformat(),
                "metadata": {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert heartbeat.status_code == 200

    nekro_payload = event_payload(
        "nekro-agent",
        source_event_id="qq:group:123:message:late-nekro",
        message_id="late-nekro",
        text="莉莉，延迟协调测试",
        real_seq="late-coordination",
        occurred_at=observed_at,
    )
    lily_payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:late-lily",
        message_id="late-lily",
        text="莉莉，延迟协调测试",
        real_seq="late-coordination",
        occurred_at=observed_at,
    )

    # The non-target times out with only one observation and therefore has
    # already continued its legacy path.
    early_peer = await client.post(
        "/v1/claims/evaluate",
        json=lily_payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "late-peer-first",
        },
    )
    assert (
        early_peer.json()["action"],
        early_peer.json()["reason"],
        early_peer.json()["enforced"],
    ) == (
        "abstain",
        "insufficient_observations",
        False,
    )

    # A later target sees both observations, but cannot turn the earlier
    # fail-open into a fictitious exclusive claim owner.
    late_target = await client.post(
        "/v1/claims/evaluate",
        json=nekro_payload,
        headers={
            "Authorization": "Bearer nekro-secret",
            "Idempotency-Key": "late-target-second",
        },
    )
    assert (
        late_target.json()["action"],
        late_target.json()["reason"],
        late_target.json()["enforced"],
    ) == (
        "abstain",
        "claim_peer_suppressions_not_acknowledged",
        False,
    )
    assert late_target.json()["features"]["coordination"] == {
        "observed_peer_instance_ids": ["lily-command"],
        "enforced_deny_instance_ids": [],
        "acknowledged_deny_instance_ids": [],
    }


async def test_claim_backfills_a_missing_legacy_decision_before_abstaining(client, app) -> None:
    app.state.settings = replace(app.state.settings, claim_mode="shadow")
    payload = event_payload(
        source_event_id="qq:group:123:message:legacy-replay",
        message_id="legacy-replay",
        real_seq="legacy-replay",
        text="莉莉",
    )
    headers = {
        "Authorization": "Bearer lily-secret",
        "Idempotency-Key": "legacy-replay-key",
    }
    ingested = await client.post("/v1/events", json=payload, headers=headers)
    assert ingested.status_code == 201
    source_event_id = ingested.json()["source_event_id"]

    async with app.state.database.sessions() as session:
        source = await session.get(SourceEvent, source_event_id)
        assert source is not None
        source.correlation_version = "qq-text-v1"
        await session.execute(delete(EventDecision).where(EventDecision.source_event_id == source_event_id))
        await session.commit()

    claimed = await client.post("/v1/claims/evaluate", json=payload, headers=headers)

    assert claimed.status_code == 200
    assert claimed.json()["reason"] == "strong_correlation_required"
    async with app.state.database.sessions() as session:
        decision = await session.scalar(select(EventDecision).where(EventDecision.source_event_id == source_event_id))
        assert decision is not None


async def test_known_bot_sender_is_observed_without_retriggering(client, app) -> None:
    nekro_instance = event_payload("nekro-agent")["instance"]
    heartbeat = await client.post(
        "/v1/heartbeats",
        json={
            "schema_version": "1.0",
            "instance": nekro_instance,
            "process_status": "running",
            "connection_status": "connected",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        },
        headers={"Authorization": "Bearer nekro-secret"},
    )
    assert heartbeat.status_code == 200

    payload = event_payload(
        "lily-command",
        source_event_id="qq:group:123:message:bot-output",
        message_id="bot-output",
        real_seq="bot-output",
        text="莉莉，wf 1+1",
    )
    payload["sender"]["id"] = nekro_instance["bot_id"]
    payload["metadata"]["native_identity"]["user_id"] = nekro_instance["bot_id"]
    observed = await client.post(
        "/v1/events",
        json=payload,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "known-bot-output",
        },
    )
    assert observed.status_code == 201

    async with app.state.database.sessions() as session:
        decision = await session.scalar(
            select(EventDecision).where(EventDecision.source_event_id == observed.json()["source_event_id"])
        )
        assert decision is not None
        assert decision.policy_version == "qq-v3-policy-v6"
        assert decision.decision_type == "observe_only"
        assert decision.reason == "bot_message_observed"
        assert decision.features_json["sender_bot_instance_id"] == "nekro-agent"


async def test_instance_token_cannot_impersonate_other_instance(client) -> None:
    response = await client.post(
        "/v1/events",
        json=event_payload("nekro-agent"),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "wrong-identity",
        },
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
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "response-without-trigger",
        },
    )
    assert response.status_code == 201, response.text
    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ResponseRecord))
        assert record is not None
        assert record.trigger_observation_id is None
        assert record.trigger_source_event_id is None


async def test_heartbeat_and_admin_views(client) -> None:
    now_value = datetime.now(timezone.utc)
    now = now_value.isoformat()
    heartbeat = {
        "schema_version": "1.0",
        "instance": event_payload()["instance"],
        "process_status": "running",
        "connection_status": "connected",
        "occurred_at": now,
        "capabilities": {
            "profile": "onebot_v11.qq.v1",
            "supported": ["send_text", "mention", "reply", "send_image"],
            "limits": {},
        },
        "metadata": {"queue_depth": 0},
    }
    sent = await client.post(
        "/v1/heartbeats",
        json=heartbeat,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert sent.status_code == 200, sent.text
    stale = await client.post(
        "/v1/heartbeats",
        json={
            **heartbeat,
            "process_status": "stopped",
            "connection_status": "disconnected",
            "occurred_at": (now_value - timedelta(days=1)).isoformat(),
        },
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert stale.status_code == 200
    assert stale.json()["reported_status"] == "online"

    denied = await client.get("/v1/instances")
    allowed = await client.get("/v1/instances", headers={"Authorization": "Bearer admin-secret"})
    assert denied.status_code == 401
    assert allowed.status_code == 200
    assert allowed.json()[0]["status"] == "online"
    assert allowed.json()[0]["capabilities"] == {
        "profile": "onebot_v11.qq.v1",
        "supported": ["mention", "reply", "send_image", "send_text"],
        "limits": {},
    }
