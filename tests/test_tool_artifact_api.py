from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from superlily_core.models import (
    RenderArtifactRecord,
    RenderDocumentRecord,
    ToolArtifact,
    ToolArtifactEvent,
    ToolAttemptEvent,
)
from superlily_core import tool_artifact_service
from superlily_core.tool_artifact_service import reap_expired_artifacts

from test_artifact_store import png_bytes
from test_tool_execution_api import (
    EXECUTABLE_DESCRIPTOR_PATH,
    PROVIDER_HEADERS,
    completion_usage,
    create_queued,
    prepare_canary,
    proof,
    pull_lease,
    successful_output,
)


def artifact_descriptor(tmp_path: Path, *, mime_type: str = "image/png") -> Path:
    value = json.loads(EXECUTABLE_DESCRIPTOR_PATH.read_text())
    value["version"] = "1.1.0"
    value["execution_permissions"]["artifacts"] = [mime_type]
    value["resource_budget"]["artifact_bytes"] = 4096
    value["required_budget_enforcement"].append("artifact_bytes")
    value["artifact_policy"] = {
        "max_count": 2,
        "max_single_bytes": 2048,
        "max_width_pixels": 16,
        "max_height_pixels": 16,
        "reservation_ttl_seconds": 30,
    }
    path = tmp_path / "artifact-status.json"
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return path


async def running_artifact_attempt(client, app, tmp_path: Path, *, key: str):
    root = tmp_path / "artifact-data"
    app.state.settings = replace(
        app.state.settings,
        artifact_root=str(root),
        artifact_secret_pepper="artifact-test-pepper-0123456789abcdef",
        artifact_orphan_grace_seconds=60,
    )
    descriptor, inventory_hash = await prepare_canary(
        client,
        app,
        descriptor_path=artifact_descriptor(tmp_path),
        budget_enforcement={
            "output_bytes": "hard",
            "wall_time": "hard",
            "artifact_bytes": "hard",
        },
    )
    invocation = await create_queued(client, descriptor, key=key)
    lease_response = await pull_lease(client, inventory_hash)
    assert lease_response.status_code == 200, lease_response.text
    lease = lease_response.json()
    started = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(lease),
        headers=PROVIDER_HEADERS,
    )
    assert started.status_code == 200, started.text
    return descriptor, invocation, lease, root


async def reserve(client, invocation: dict, lease: dict, *, key: str, body: bytes):
    return await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/artifacts/reserve",
        json={
            **proof(lease),
            "mime_type": "image/png",
            "declared_bytes": len(body),
            "declared_sha256": hashlib.sha256(body).hexdigest(),
        },
        headers={**PROVIDER_HEADERS, "Idempotency-Key": key},
    )


async def upload(client, reservation: dict, body: bytes, *, secret: str | None = None):
    return await client.put(
        f"/v1/tool-artifacts/{reservation['artifact_id']}/content",
        content=body,
        headers={
            **PROVIDER_HEADERS,
            "Content-Type": "image/png",
            "X-Superlily-Artifact-Upload-Secret": secret or reservation["upload_secret"],
        },
    )


async def finalize(client, invocation: dict, lease: dict, uploaded: dict):
    return await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/artifacts/finalize",
        json={
            **proof(lease),
            **{key: value for key, value in uploaded.items() if key != "state"},
        },
        headers=PROVIDER_HEADERS,
    )


async def test_artifact_happy_path_is_idempotent_fenced_and_completed_atomically(
    client, app, tmp_path: Path
) -> None:
    descriptor, invocation, lease, root = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-happy-invocation"
    )
    body = png_bytes(2, 3)
    first = await reserve(client, invocation, lease, key="artifact-reserve-happy", body=body)
    assert first.status_code == 201, first.text
    replay = await reserve(client, invocation, lease, key="artifact-reserve-happy", body=body)
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert replay.json()["upload_secret"] == first.json()["upload_secret"]
    uploaded = await upload(client, first.json(), body)
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["content_sha256"] == hashlib.sha256(body).hexdigest()
    assert (uploaded.json()["width_pixels"], uploaded.json()["height_pixels"]) == (2, 3)
    reused = await upload(client, first.json(), body)
    assert reused.status_code == 409

    finalized = await finalize(client, invocation, lease, uploaded.json())
    assert finalized.status_code == 200, finalized.text
    finalized_replay = await finalize(client, invocation, lease, uploaded.json())
    assert finalized_replay.status_code == 200
    assert finalized_replay.json() == finalized.json()

    output = successful_output(descriptor.descriptor_hash)
    usage = completion_usage(output)
    usage["artifact_bytes"] = len(body)
    completed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "artifact-result-happy",
            "output": output,
            "usage": usage,
            "artifacts": [finalized.json()],
        },
        headers=PROVIDER_HEADERS,
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["state"] == "succeeded"
    async with app.state.database.sessions() as session:
        stored = await session.get(ToolArtifact, first.json()["artifact_id"])
        events = (
            await session.scalars(
                select(ToolArtifactEvent)
                .where(ToolArtifactEvent.artifact_id == stored.id)
                .order_by(ToolArtifactEvent.sequence)
            )
        ).all()
        attempt_events = (
            await session.scalars(
                select(ToolAttemptEvent).where(ToolAttemptEvent.attempt_id == lease["attempt_id"])
            )
        ).all()
    assert stored is not None and stored.referenced_at is not None
    finalize_event = next(event for event in events if event.event == "finalize")
    finalized_at = stored.finalized_at
    assert finalized_at is not None
    if finalized_at.tzinfo is None:
        finalized_at = finalized_at.replace(tzinfo=timezone.utc)
    effective_at = finalize_event.effective_at
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=timezone.utc)
    assert finalized_at == effective_at
    assert [event.event for event in events] == [
        "reserve",
        "upload_start",
        "upload_complete",
        "finalize",
        "reference",
    ]
    serialized = json.dumps(
        [event.evidence_json for event in events]
        + [event.evidence_json for event in attempt_events],
        ensure_ascii=False,
    )
    assert first.json()["upload_secret"] not in serialized
    assert lease["lease_secret"] not in serialized
    assert not any(path.suffix == ".part" for path in (root / "quarantine").iterdir())
    assert (root / stored.storage_key).is_file()


async def test_artifact_wrong_secret_mime_and_invalid_body_fail_closed_without_secret_leak(
    client, app, tmp_path: Path
) -> None:
    _, invocation, lease, _ = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-reject-invocation"
    )
    body = png_bytes()
    reservation = (
        await reserve(client, invocation, lease, key="artifact-reserve-reject", body=body)
    ).json()
    wrong = await upload(client, reservation, body, secret="x" * 43)
    assert wrong.status_code == 401
    assert "x" * 43 not in wrong.text
    wrong_mime = await client.put(
        f"/v1/tool-artifacts/{reservation['artifact_id']}/content",
        content=body,
        headers={
            **PROVIDER_HEADERS,
            "Content-Type": "application/octet-stream",
            "X-Superlily-Artifact-Upload-Secret": reservation["upload_secret"],
        },
    )
    assert wrong_mime.status_code == 409
    accepted = await upload(client, reservation, body)
    assert accepted.status_code == 200, accepted.text

    bad_body = b"not-a-png"
    bad = (
        await reserve(client, invocation, lease, key="artifact-reserve-bad-body", body=bad_body)
    ).json()
    rejected = await upload(client, bad, bad_body)
    assert rejected.status_code == 409
    async with app.state.database.sessions() as session:
        bad_row = await session.get(ToolArtifact, bad["artifact_id"])
        good_row = await session.get(ToolArtifact, reservation["artifact_id"])
    assert bad_row is not None and bad_row.state == "rejected"
    assert good_row is not None and good_row.state == "uploading"
    assert bad["upload_secret"] not in rejected.text


async def test_execution_validation_errors_redact_provider_secrets(client) -> None:
    leaked = "short-secret-that-must-not-be-echoed"
    response = await client.post(
        "/v1/tool-executions/invocation-unknown/artifacts/reserve",
        json={
            "attempt_id": "attempt-unknown",
            "fencing_token": 1,
            "lease_secret": leaked,
            "mime_type": "Image/PNG",
        },
        headers={**PROVIDER_HEADERS, "Idempotency-Key": "artifact-invalid-proof"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid provider execution request"}
    assert leaked not in response.text
    assert response.headers["Cache-Control"] == "no-store"


async def test_completion_requires_finalized_exact_references_and_exact_usage(
    client, app, tmp_path: Path
) -> None:
    descriptor, invocation, lease, _ = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-complete-invalid-invocation"
    )
    body = png_bytes()
    reservation = (
        await reserve(client, invocation, lease, key="artifact-reserve-not-final", body=body)
    ).json()
    uploaded = (await upload(client, reservation, body)).json()
    output = successful_output(descriptor.descriptor_hash)
    usage = completion_usage(output)
    usage["artifact_bytes"] = len(body)
    premature = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "artifact-result-premature",
            "output": output,
            "usage": usage,
            "artifacts": [
                {key: value for key, value in uploaded.items() if key != "state"}
            ],
        },
        headers=PROVIDER_HEADERS,
    )
    assert premature.status_code == 200, premature.text
    assert premature.json()["state"] == "failed"
    assert premature.json()["error_code"] == "artifact_failed"
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
    assert row is not None and row.referenced_at is None


async def test_artifact_reaper_expires_quarantine_and_removes_untracked_objects(
    client, app, tmp_path: Path, monkeypatch
) -> None:
    _, invocation, lease, root = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-reaper-invocation"
    )
    body = png_bytes()
    reservation = (
        await reserve(client, invocation, lease, key="artifact-reserve-reaper", body=body)
    ).json()
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
        assert row is not None
        future = row.expires_at + timedelta(seconds=1)
        if future.tzinfo is None:
            future = future.replace(tzinfo=timezone.utc)
    async def future_database_now(session):
        return future

    monkeypatch.setattr(tool_artifact_service, "database_now", future_database_now)
    orphan_key = "objects/sha256/" + "f" * 2 + "/" + "f" * 64
    orphan = root / orphan_key
    orphan.parent.mkdir(parents=True, mode=0o700)
    orphan.write_bytes(b"orphan")
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).timestamp()
    orphan.chmod(0o600)
    import os

    os.utime(orphan, (old, old))
    async with app.state.database.sessions() as session:
        assert await reap_expired_artifacts(session, app.state.settings) == [
            reservation["artifact_id"]
        ]
    assert not orphan.exists()
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
    assert row is not None and row.state == "expired"


async def test_artifact_reaper_preserves_live_render_document_objects(
    app, tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "shared-artifact-data"
    app.state.settings = replace(
        app.state.settings,
        artifact_root=str(root),
        artifact_secret_pepper="render-reaper-test-pepper-0123456789",
        artifact_orphan_grace_seconds=60,
    )
    body = png_bytes()
    digest = hashlib.sha256(body).hexdigest()
    storage_key = f"objects/sha256/{digest[:2]}/{digest}"
    object_path = root / storage_key
    object_path.parent.mkdir(parents=True, mode=0o700)
    object_path.write_bytes(body)
    object_path.chmod(0o600)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).timestamp()
    import os

    os.utime(object_path, (old, old))
    now = datetime.now(timezone.utc)
    async with app.state.database.sessions() as session:
        document = RenderDocumentRecord(
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_1080353942",
            source_event_id="render-reaper-regression",
            idempotency_key="render-reaper-regression",
            request_sha256="a" * 64,
            document_json={"schema_version": "1.0"},
            status="succeeded",
            render_duration_ms=1,
            completed_at=now,
        )
        session.add(document)
        await session.flush()
        session.add(
            RenderArtifactRecord(
                render_id=document.id,
                content_sha256=digest,
                storage_key=storage_key,
                mime_type="image/png",
                byte_size=len(body),
                width_pixels=1,
                height_pixels=1,
                created_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        await session.commit()

    async def current_database_now(session):
        return now

    monkeypatch.setattr(tool_artifact_service, "database_now", current_database_now)
    async with app.state.database.sessions() as session:
        await reap_expired_artifacts(session, app.state.settings)
    assert object_path.is_file()


async def test_artifact_reaper_deletes_bytes_only_after_referenced_retention(
    client, app, tmp_path: Path, monkeypatch
) -> None:
    descriptor, invocation, lease, root = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-retention-invocation"
    )
    body = png_bytes()
    reservation = (
        await reserve(client, invocation, lease, key="artifact-reserve-retention", body=body)
    ).json()
    uploaded = (await upload(client, reservation, body)).json()
    reference_response = await finalize(client, invocation, lease, uploaded)
    assert reference_response.status_code == 200, reference_response.text
    output = successful_output(descriptor.descriptor_hash)
    usage = completion_usage(output)
    usage["artifact_bytes"] = len(body)
    complete = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "artifact-result-retention",
            "output": output,
            "usage": usage,
            "artifacts": [reference_response.json()],
        },
        headers=PROVIDER_HEADERS,
    )
    assert complete.status_code == 200 and complete.json()["state"] == "succeeded"
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
        assert row is not None and row.referenced_at is not None and row.storage_key is not None
        object_path = root / row.storage_key
        retention = row.policy_snapshot_json["result_retention_seconds"]
        future = row.referenced_at + timedelta(seconds=retention + 1)
        if future.tzinfo is None:
            future = future.replace(tzinfo=timezone.utc)
    assert object_path.is_file()

    async def future_database_now(session):
        return future

    monkeypatch.setattr(tool_artifact_service, "database_now", future_database_now)
    async with app.state.database.sessions() as session:
        await reap_expired_artifacts(session, app.state.settings)
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
        events = (
            await session.scalars(
                select(ToolArtifactEvent)
                .where(ToolArtifactEvent.artifact_id == reservation["artifact_id"])
                .order_by(ToolArtifactEvent.sequence)
            )
        ).all()
    assert row is not None and row.content_deleted_at is not None
    deleted_at = row.content_deleted_at
    if deleted_at.tzinfo is None:
        deleted_at = deleted_at.replace(tzinfo=timezone.utc)
    assert deleted_at == future
    assert events[-1].event == "cleanup"
    assert events[-1].reason_code == "artifact_retention_elapsed"
    assert not object_path.exists()


async def test_finalize_database_failure_leaves_no_visible_success_and_orphan_is_reaped(
    client, app, tmp_path: Path, monkeypatch
) -> None:
    _, invocation, lease, root = await running_artifact_attempt(
        client, app, tmp_path, key="artifact-finalize-fault-invocation"
    )
    body = png_bytes()
    reservation = (
        await reserve(client, invocation, lease, key="artifact-reserve-finalize-fault", body=body)
    ).json()
    uploaded = (await upload(client, reservation, body)).json()
    original_transition = tool_artifact_service._transition_artifact

    async def fail_finalize_transition(*args, **kwargs):
        if kwargs.get("event") == "finalize":
            raise RuntimeError("simulated database failure after object publish")
        return await original_transition(*args, **kwargs)

    monkeypatch.setattr(
        tool_artifact_service,
        "_transition_artifact",
        fail_finalize_transition,
    )
    with pytest.raises(RuntimeError, match="simulated database failure"):
        await finalize(client, invocation, lease, uploaded)
    object_path = root / "objects" / "sha256" / uploaded["content_sha256"][:2] / uploaded[
        "content_sha256"
    ]
    assert object_path.is_file()
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
    assert row is not None and row.state == "uploading" and row.storage_key is None

    monkeypatch.setattr(
        tool_artifact_service,
        "_transition_artifact",
        original_transition,
    )
    recovery = await finalize(client, invocation, lease, uploaded)
    assert recovery.status_code == 409
    async with app.state.database.sessions() as session:
        row = await session.get(ToolArtifact, reservation["artifact_id"])
    assert row is not None and row.state == "rejected"
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).timestamp()
    import os

    os.utime(object_path, (old, old))
    async with app.state.database.sessions() as session:
        await reap_expired_artifacts(session, app.state.settings)
    assert not object_path.exists()


async def test_artifact_descriptor_is_ineligible_when_store_is_disabled(
    client, app, tmp_path: Path
) -> None:
    assert app.state.settings.artifact_enabled is False
    descriptor, _ = await prepare_canary(
        client,
        app,
        descriptor_path=artifact_descriptor(tmp_path),
        budget_enforcement={
            "output_bytes": "hard",
            "wall_time": "hard",
            "artifact_bytes": "hard",
        },
    )
    assert app.state.settings.artifact_enabled is False
    view = await client.get(
        f"/v1/tools/{descriptor.tool_id}", headers={"Authorization": "Bearer admin-secret"}
    )
    assert view.status_code == 200
    assert "artifact_storage_unavailable" in view.json()["versions"][0]["effective"]["reasons"], view.text
