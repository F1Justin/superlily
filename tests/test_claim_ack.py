from dataclasses import replace
from datetime import datetime, timezone

from superlily_core.command_registry import runtime_registry_snapshot_hash


def _event_payload(
    instance_id: str,
    *,
    event_label: str,
    real_seq: str,
    occurred_at: datetime,
    text: str = "wf 1+1",
    sender_id: str = "789",
    references: list[dict] | None = None,
    segments: list[dict] | None = None,
) -> dict:
    bot_id = "985393579" if instance_id == "lily-command" else "2022692714"
    message_id = f"short-{event_label}-{instance_id}"
    return {
        "schema_version": "1.0",
        "source_event_id": f"qq:source:v2:{event_label}:{instance_id}",
        "instance": {
            "instance_id": instance_id,
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": bot_id,
            "role": "command" if instance_id == "lily-command" else "talk",
        },
        "event_type": "message",
        "conversation": {"id": "123", "type": "group", "name": "Claim Ack Test"},
        "sender": {"id": sender_id, "name": "Tester", "roles": []},
        "message": {
            "id": message_id,
            "text": text,
            "segments": (
                segments
                if segments is not None
                else [{"type": "text", "data": {"text": text}}]
            ),
            "attachments": [],
        },
        "references": references or [],
        "occurred_at": occurred_at.isoformat(),
        "metadata": {
            "native_identity": {
                "schema": "onebot_v11.qq.native_identity.v1",
                "message_id": message_id,
                "real_seq": real_seq,
                "group_id": "123",
                "user_id": sender_id,
                "time": str(int(occurred_at.timestamp())),
            }
        },
    }


async def _ready_claim_runtime(client, app, observed_at: datetime) -> None:
    app.state.settings = replace(
        app.state.settings,
        claim_mode="canary",
        claim_canary_conversations=frozenset({"qq:group:123"}),
        claim_coalesce_milliseconds=0,
    )
    plugins = [
        {
            "plugin_id": "wolfram",
            "module_name": "plugins.wolfram",
            "display_name": "Wolfram",
            "matcher_count": 1,
            "classified_matcher_count": 1,
        }
    ]
    candidates = [
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
    ]
    snapshot = {
        "schema_version": "1.0",
        "instance": _event_payload(
            "lily-command",
            event_label="registry",
            real_seq="registry",
            occurred_at=observed_at,
        )["instance"],
        "snapshot_hash": runtime_registry_snapshot_hash(plugins, candidates),
        "observed_at": observed_at.isoformat(),
        "plugins": plugins,
        "candidates": candidates,
    }
    created = await client.post(
        "/v1/command-registry/snapshots",
        json=snapshot,
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert created.status_code == 201, created.text

    instances = (("lily-command", "lily-secret"), ("nekro-agent", "nekro-secret"))
    for instance_id, token in instances:
        heartbeat = await client.post(
            "/v1/heartbeats",
            json={
                "schema_version": "1.0",
                "instance": _event_payload(
                    instance_id,
                    event_label="heartbeat",
                    real_seq="heartbeat",
                    occurred_at=observed_at,
                )["instance"],
                "process_status": "running",
                "connection_status": "connected",
                "occurred_at": observed_at.isoformat(),
                "metadata": {},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert heartbeat.status_code == 200, heartbeat.text


async def _observe_pair(client, *, label: str, real_seq: str, observed_at: datetime) -> tuple[dict, dict]:
    lily = _event_payload(
        "lily-command",
        event_label=label,
        real_seq=real_seq,
        occurred_at=observed_at,
    )
    nekro = _event_payload(
        "nekro-agent",
        event_label=label,
        real_seq=real_seq,
        occurred_at=observed_at,
    )
    for payload, token, key in (
        (lily, "lily-secret", f"event-{label}-lily"),
        (nekro, "nekro-secret", f"event-{label}-nekro"),
    ):
        response = await client.post(
            "/v1/events",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        )
        assert response.status_code == 201, response.text
    return lily, nekro


async def test_enforced_allow_requires_acknowledged_peer_suppression(client, app) -> None:
    observed_at = datetime.now(timezone.utc)
    await _ready_claim_runtime(client, app, observed_at)

    # A committed deny is not proof that the bridge received it and installed
    # suppression.  Simulate the response being lost by deliberately not acking.
    lily_lost, nekro_lost = await _observe_pair(
        client,
        label="lost-deny-response",
        real_seq="881001",
        observed_at=observed_at,
    )
    peer_deny = await client.post(
        "/v1/claims/evaluate",
        json=nekro_lost,
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "claim-lost-nekro"},
    )
    assert peer_deny.status_code == 200, peer_deny.text
    assert (peer_deny.json()["action"], peer_deny.json()["enforced"]) == ("deny", True)
    assert peer_deny.json()["acknowledged_at"] is None

    uncoordinated_target = await client.post(
        "/v1/claims/evaluate",
        json=lily_lost,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "claim-lost-lily"},
    )
    assert uncoordinated_target.status_code == 200, uncoordinated_target.text
    assert (
        uncoordinated_target.json()["action"],
        uncoordinated_target.json()["reason"],
        uncoordinated_target.json()["enforced"],
    ) == ("abstain", "claim_peer_suppressions_not_acknowledged", False)
    assert uncoordinated_target.json()["features"]["coordination"] == {
        "observed_peer_instance_ids": ["nekro-agent"],
        "enforced_deny_instance_ids": ["nekro-agent"],
        "acknowledged_deny_instance_ids": [],
    }

    # Use a fresh event because claims are immutable per source/instance.  Ack
    # the peer suppression before the target's first evaluation.
    lily_acked, nekro_acked = await _observe_pair(
        client,
        label="acked-deny-response",
        real_seq="881002",
        observed_at=observed_at,
    )
    ackable_deny = await client.post(
        "/v1/claims/evaluate",
        json=nekro_acked,
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "claim-acked-nekro"},
    )
    assert (ackable_deny.json()["action"], ackable_deny.json()["enforced"]) == ("deny", True)
    claim_id = ackable_deny.json()["claim_id"]

    missing_key = await client.post(
        f"/v1/claims/{claim_id}/ack",
        headers={"Authorization": "Bearer nekro-secret"},
    )
    assert missing_key.status_code == 422
    wrong_identity = await client.post(
        f"/v1/claims/{claim_id}/ack",
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "ack-wrong-identity"},
    )
    assert wrong_identity.status_code == 403

    first_ack = await client.post(
        f"/v1/claims/{claim_id}/ack",
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "ack-nekro-first"},
    )
    repeated_ack = await client.post(
        f"/v1/claims/{claim_id}/ack",
        headers={"Authorization": "Bearer nekro-secret", "Idempotency-Key": "ack-nekro-retry"},
    )
    assert first_ack.status_code == repeated_ack.status_code == 200
    assert first_ack.json()["duplicate"] is False
    assert repeated_ack.json()["duplicate"] is True
    assert repeated_ack.json()["acknowledged_at"] == first_ack.json()["acknowledged_at"]

    coordinated_target = await client.post(
        "/v1/claims/evaluate",
        json=lily_acked,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "claim-acked-lily"},
    )
    assert coordinated_target.status_code == 200, coordinated_target.text
    assert (coordinated_target.json()["action"], coordinated_target.json()["enforced"]) == (
        "allow",
        True,
    )
    assert coordinated_target.json()["features"]["coordination"] == {
        "observed_peer_instance_ids": ["nekro-agent"],
        "enforced_deny_instance_ids": ["nekro-agent"],
        "acknowledged_deny_instance_ids": ["nekro-agent"],
    }

    non_deny_ack = await client.post(
        f"/v1/claims/{coordinated_target.json()['claim_id']}/ack",
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "ack-allow-invalid"},
    )
    assert non_deny_ack.status_code == 409

    context = await client.get(
        f"/v1/events/{coordinated_target.json()['source_event_id']}/context",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert context.status_code == 200
    claims_by_instance = {item["instance_id"]: item for item in context.json()["claims"]}
    assert claims_by_instance["nekro-agent"]["acknowledged_at"] is not None
    assert claims_by_instance["lily-command"]["acknowledged_at"] is None

    summary = await client.get(
        "/v1/claims/summary?hours=1",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert summary.status_code == 200
    assert summary.json()["acknowledged"] == {"deny": 1}


async def test_reply_to_other_without_summon_enforces_acknowledged_denies_for_both_bots(
    client,
    app,
) -> None:
    observed_at = datetime.now(timezone.utc)
    await _ready_claim_runtime(client, app, observed_at)
    instances = (("lily-command", "lily-secret"), ("nekro-agent", "nekro-secret"))

    parent_by_instance: dict[str, dict] = {}
    for instance_id, token in instances:
        parent = _event_payload(
            instance_id,
            event_label="human-parent",
            real_seq="882001",
            occurred_at=observed_at,
            text="需要被引用解释的内容",
            sender_id="456",
        )
        response = await client.post(
            "/v1/events",
            json=parent,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"human-parent-{instance_id}",
            },
        )
        assert response.status_code == 201, response.text
        parent_by_instance[instance_id] = parent

    child_by_instance: dict[str, dict] = {}
    for instance_id, token in instances:
        parent_message_id = parent_by_instance[instance_id]["message"]["id"]
        child = _event_payload(
            instance_id,
            event_label="reply-other-command-looking",
            real_seq="882002",
            occurred_at=observed_at,
            text="今日老婆",
            references=[
                {
                    "type": "reply_to",
                    "platform_message_id": parent_message_id,
                    "conversation_id": "123",
                    "conversation_type": "group",
                    "sender_id": "456",
                }
            ],
            segments=[
                {"type": "reply", "data": {"id": parent_message_id}},
                {"type": "at", "data": {"qq": "456"}},
                {"type": "text", "data": {"text": " 今日老婆"}},
            ],
        )
        response = await client.post(
            "/v1/events",
            json=child,
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"reply-other-event-{instance_id}",
            },
        )
        assert response.status_code == 201, response.text
        child_by_instance[instance_id] = child

    claims: dict[str, dict] = {}
    for instance_id, token in instances:
        response = await client.post(
            "/v1/claims/evaluate",
            json=child_by_instance[instance_id],
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"reply-other-claim-{instance_id}",
            },
        )
        assert response.status_code == 200, response.text
        claim = response.json()
        assert (claim["action"], claim["reason"], claim["ready"], claim["enforced"]) == (
            "deny",
            "decision_suppress_all:reply_to_other_observed",
            True,
            True,
        )
        assert claim["features"]["gates"]["suppression_scope"] == "all_instances"

        acknowledged = await client.post(
            f"/v1/claims/{claim['claim_id']}/ack",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": f"reply-other-ack-{instance_id}",
            },
        )
        assert acknowledged.status_code == 200, acknowledged.text
        assert acknowledged.json()["acknowledged_at"] is not None
        claims[instance_id] = claim

    context = await client.get(
        f"/v1/events/{claims['lily-command']['source_event_id']}/context",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert context.status_code == 200, context.text
    context_claims = {item["instance_id"]: item for item in context.json()["claims"]}
    assert set(context_claims) == {"lily-command", "nekro-agent"}
    assert all(item["action"] == "deny" for item in context_claims.values())
    assert all(item["acknowledged_at"] is not None for item in context_claims.values())


async def test_reply_to_other_suppresses_single_observer_lily_only_group(client, app) -> None:
    observed_at = datetime.now(timezone.utc)
    await _ready_claim_runtime(client, app, observed_at)

    parent = _event_payload(
        "lily-command",
        event_label="single-observer-human-parent",
        real_seq="883001",
        occurred_at=observed_at,
        text="只有 Lily 收到的普通人消息",
        sender_id="456",
    )
    parent_response = await client.post(
        "/v1/events",
        json=parent,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "single-observer-human-parent",
        },
    )
    assert parent_response.status_code == 201, parent_response.text

    parent_message_id = parent["message"]["id"]
    child = _event_payload(
        "lily-command",
        event_label="single-observer-reply-other",
        real_seq="883002",
        occurred_at=observed_at,
        text="今日老婆",
        references=[
            {
                "type": "reply_to",
                "platform_message_id": parent_message_id,
                "conversation_id": "123",
                "conversation_type": "group",
                "sender_id": "456",
            }
        ],
        segments=[
            {"type": "reply", "data": {"id": parent_message_id}},
            {"type": "at", "data": {"qq": "456"}},
            {"type": "text", "data": {"text": " 今日老婆"}},
        ],
    )
    response = await client.post(
        "/v1/claims/evaluate",
        json=child,
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "single-observer-reply-other-claim",
        },
    )
    assert response.status_code == 200, response.text
    claim = response.json()
    assert (claim["action"], claim["reason"], claim["ready"], claim["enforced"]) == (
        "deny",
        "decision_suppress_all:reply_to_other_observed",
        True,
        True,
    )
    assert claim["features"]["gates"]["observation_count"] == 1
    assert claim["features"]["gates"]["effective_required_observations"] == 1

    acknowledged = await client.post(
        f"/v1/claims/{claim['claim_id']}/ack",
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "single-observer-reply-other-ack",
        },
    )
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["acknowledged_at"] is not None
