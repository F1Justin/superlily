from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

import pytest
from sqlalchemy import update

from superlily_contracts import (
    ProviderInventoryTool,
    ProviderRegistration,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
    load_tool_rollout_plan,
    provider_inventory_snapshot_hash,
)
from superlily_core.document_renderer_client import (
    DocumentRendererClient,
    RenderedDocument,
)
from superlily_core.models import (
    BotInstance,
    RenderArtifactRecord,
    ToolArtifact,
    ToolDescriptorLifecycleEvent,
    ToolDescriptorRecord,
    ToolProvider,
    ToolRolloutPlanLifecycleEvent,
    ToolRolloutPlanRecord,
)
from superlily_core.rollout_service import import_tool_rollout_plan
from superlily_core.tool_registry_service import (
    import_tool_descriptor,
    register_tool_provider,
)

from test_artifact_store import png_bytes


ROOT = Path(__file__).parents[1]
WOLFRAM_DESCRIPTOR = ROOT / "registry/descriptors/wolfram.run/1.0.0.json"
LATEX_DESCRIPTOR = ROOT / "registry/descriptors/latex.render/1.0.0.json"


async def _prepare_tool(
    client,
    app,
    *,
    descriptor_path: Path,
    provider_id: str,
    provider_token: str,
    implementation_hash: str,
    budget_enforcement: dict[str, str],
) -> tuple[ToolDescriptorRecord, str]:
    app.state.settings = replace(
        app.state.settings,
        provider_tokens={
            **app.state.settings.provider_tokens,
            provider_id: provider_token,
        },
    )
    source = descriptor_path.read_bytes()
    authority = load_tool_descriptor(source).authority
    async with app.state.database.sessions() as session:
        descriptor, duplicate = await import_tool_descriptor(
            session,
            source,
            source_commit="5" * 40,
            bundle_hash=authority.sha256,
            reviewer="phase4-compatibility-reviewer",
        )
        assert duplicate is False
        stored = await session.get(ToolDescriptorRecord, descriptor.id)
        assert stored is not None
        session.add(
            ToolDescriptorLifecycleEvent(
                descriptor_id=stored.id,
                sequence=2,
                previous_lifecycle=stored.lifecycle,
                lifecycle="active",
                actor="phase4-test-reviewer",
                reason="activate exact compatibility descriptor",
            )
        )
        await session.flush()
        await session.execute(
            update(ToolDescriptorRecord)
            .where(ToolDescriptorRecord.id == stored.id)
            .values(lifecycle="active", resource_version=2)
        )
        await session.commit()

    async with app.state.database.sessions() as session:
        provider, duplicate = await register_tool_provider(
            session,
            ProviderRegistration(
                provider_id=provider_id,
                owner="superlily-operations",
                lifecycle="active",
                allowed_protocols=["superlily-provider-pull-v1"],
                tool_selectors=[descriptor.tool_id],
            ),
            actor="phase4-test-reviewer",
            settings=app.state.settings,
        )
        assert duplicate is False
        stored_provider = await session.get(ToolProvider, provider.id)
        assert stored_provider is not None

    inventory_tool = ProviderInventoryTool(
        tool_id=descriptor.tool_id,
        descriptor_version=descriptor.version,
        descriptor_hash=descriptor.descriptor_hash,
        protocol_version="superlily-provider-pull-v1",
        implementation_hash=implementation_hash,
        budget_enforcement=budget_enforcement,
    )
    inventory_hash = provider_inventory_snapshot_hash(
        provider_id=provider_id,
        protocol_version="superlily-provider-pull-v1",
        tools=[inventory_tool],
    )
    headers = {"Authorization": f"Bearer {provider_token}"}
    observed_at = datetime.now(timezone.utc).isoformat()
    inventory = await client.post(
        "/v1/provider-inventory/snapshots",
        json={
            "schema_version": "1.0",
            "provider_id": provider_id,
            "snapshot_hash": inventory_hash,
            "observed_at": observed_at,
            "protocol_version": "superlily-provider-pull-v1",
            "tools": [inventory_tool.model_dump(mode="json")],
        },
        headers={**headers, "Idempotency-Key": f"{provider_id}-inventory"},
    )
    assert inventory.status_code == 201, inventory.text
    heartbeat = await client.post(
        "/v1/providers/heartbeats",
        json={
            "schema_version": "1.0",
            "provider_id": provider_id,
            "inventory_hash": inventory_hash,
            "observed_at": observed_at,
            "health": "healthy",
            "current_concurrency": 0,
            "max_concurrency": 1,
            "metadata": {},
        },
        headers=headers,
    )
    assert heartbeat.status_code == 200, heartbeat.text

    app.state.settings = replace(
        app.state.settings,
        tool_execution_mode="canary",
        tool_lease_seconds=10,
    )
    plan_now = datetime.now(timezone.utc)
    plan_source = json.dumps(
        {
            "schema_version": "1.0",
            "plan_id": f"{descriptor.tool_id.replace('.', '-')}-phase4-command",
            "version": "1.0.0",
            "mode": "canary",
            "starts_at": (plan_now - timedelta(minutes=1)).isoformat(),
            "expires_at": (plan_now + timedelta(hours=1)).isoformat(),
            "max_invocations": 100,
            "rollback_mode": "ledger_only",
            "reason": "Phase 4 exact command compatibility test",
            "items": [
                {
                    "item_id": "exact-command",
                    "tool_id": descriptor.tool_id,
                    "descriptor_version": descriptor.version,
                    "descriptor_hash": descriptor.descriptor_hash,
                    "canonical_conversation": "qq:group:1080353942",
                    "caller": "command",
                    "provider_id": provider_id,
                    "expected_descriptor_resource_version": 2,
                    "expected_provider_resource_version": 1,
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    async with app.state.database.sessions() as session:
        plan, duplicate = await import_tool_rollout_plan(
            session,
            plan_source,
            source_commit="6" * 40,
            bundle_hash=load_tool_rollout_plan(plan_source).authority.sha256,
            reviewer="phase4-compatibility-reviewer",
        )
        assert duplicate is False
        session.add(
            ToolRolloutPlanLifecycleEvent(
                plan_record_id=plan.id,
                sequence=2,
                previous_lifecycle="reviewed",
                lifecycle="active",
                actor="phase4-test-operator",
                reason="activate exact command canary",
            )
        )
        await session.flush()
        await session.execute(
            update(ToolRolloutPlanRecord)
            .where(ToolRolloutPlanRecord.id == plan.id)
            .values(lifecycle="active", resource_version=2, updated_at=plan_now)
        )
        await session.commit()
    return descriptor, inventory_hash


async def _add_command_instance(app) -> None:
    async with app.state.database.sessions() as session:
        session.add(
            BotInstance(
                id="lily-command",
                platform="qq",
                adapter="onebot_v11",
                bot_id="123",
                role="command",
                metadata_json={
                    "capabilities": {
                        "profile": "onebot_v11.qq.v1",
                        "supported": ["send_image", "send_text"],
                        "limits": {},
                    }
                },
            )
        )
        await session.commit()


async def _invoke_and_start(
    client,
    *,
    descriptor: ToolDescriptorRecord,
    inventory_hash: str,
    provider_token: str,
    tool_input: dict,
    key: str,
) -> tuple[dict, dict]:
    invocation = await client.post(
        "/v1/tool-invocations",
        json={
            "schema_version": "1.0",
            "tool_id": descriptor.tool_id,
            "descriptor_version": descriptor.version,
            "descriptor_hash": descriptor.descriptor_hash,
            "input": tool_input,
            "principal": {
                "platform": "qq",
                "sender_id": "123456",
                "conversation_id": "group:1080353942",
                "conversation_type": "group",
                "platform_roles": ["member"],
                "source_event_id": f"qq:message:{key}",
                "entry_id": f"command:{key}",
            },
            "capabilities": [],
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": key,
        },
    )
    assert invocation.status_code == 201, invocation.text
    headers = {"Authorization": f"Bearer {provider_token}"}
    lease_response = await client.post(
        "/v1/tool-executions/lease",
        json={"schema_version": "1.0", "inventory_hash": inventory_hash},
        headers=headers,
    )
    assert lease_response.status_code == 200, lease_response.text
    lease = lease_response.json()
    proof = {
        "schema_version": "1.0",
        "attempt_id": lease["attempt_id"],
        "fencing_token": lease["fencing_token"],
        "lease_secret": lease["lease_secret"],
    }
    started = await client.post(
        f"/v1/tool-executions/{invocation.json()['invocation_id']}/start",
        json=proof,
        headers=headers,
    )
    assert started.status_code == 200, started.text
    return invocation.json(), {**lease, **proof}


def _usage(tool_input: dict, output: dict, *, artifact_bytes: int = 0) -> dict:
    return ToolUsage(
        wall_time_ms=1,
        cpu_ms=1,
        memory_peak_bytes=1_048_576,
        input_bytes=len(canonicalize_json_value(tool_input).canonical_bytes),
        output_bytes=len(canonicalize_json_value(output).canonical_bytes),
        artifact_bytes=artifact_bytes,
    ).model_dump(mode="json")


def _render_settings(app, tmp_path: Path):
    return replace(
        app.state.settings,
        artifact_root=str(tmp_path / "phase4-tool-artifacts"),
        artifact_secret_pepper="phase4-tool-result-pepper-0123456789",
        render_mode="canary",
        render_canary_conversations=frozenset(
            {"onebot_v11-group_1080353942"}
        ),
        render_backend_url="http://document-renderer:8000",
        render_backend_token="r" * 32,
        render_implementation_hash="8" * 64,
    )


@pytest.mark.asyncio
async def test_wolfram_command_result_uses_the_same_render_pipeline(
    client, app, tmp_path, monkeypatch
) -> None:
    body = png_bytes(5, 4)

    async def render_document(self, document, *, timeout_seconds):
        del self, timeout_seconds
        assert document.blocks[0].kind == "code"
        assert document.blocks[0].code == "(-1 + x) (1 + x)"
        return RenderedDocument(
            body,
            sha256(body).hexdigest(),
            5,
            4,
        )

    monkeypatch.setattr(DocumentRendererClient, "render_document", render_document)
    app.state.settings = _render_settings(app, tmp_path)
    await _add_command_instance(app)
    token = "wolfram-provider-secret"
    descriptor, inventory_hash = await _prepare_tool(
        client,
        app,
        descriptor_path=WOLFRAM_DESCRIPTOR,
        provider_id="provider-wolfram-primary",
        provider_token=token,
        implementation_hash="7" * 64,
        budget_enforcement={
            "wall_time": "hard",
            "memory": "hard",
            "input_bytes": "hard",
            "output_bytes": "hard",
        },
    )
    tool_input = {"expression": "Factor[x^2-1]"}
    invocation, lease = await _invoke_and_start(
        client,
        descriptor=descriptor,
        inventory_hash=inventory_hash,
        provider_token=token,
        tool_input=tool_input,
        key="phase4-wolfram-command",
    )
    output = {"kind": "text", "text": "(-1 + x) (1 + x)"}
    provider_headers = {"Authorization": f"Bearer {token}"}
    completed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            "schema_version": "1.0",
            "attempt_id": lease["attempt_id"],
            "fencing_token": lease["fencing_token"],
            "lease_secret": lease["lease_secret"],
            "provider_result_id": "phase4-wolfram-result",
            "output": output,
            "usage": _usage(tool_input, output),
        },
        headers=provider_headers,
    )
    assert completed.status_code == 200, completed.text
    rendered = await client.post(
        f"/v1/tool-invocations/{invocation['invocation_id']}/render-result",
        json={
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "conversation_key": "onebot_v11-group_1080353942",
            "source_event_id": "qq:message:phase4-wolfram-command",
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-wolfram-render",
        },
    )
    assert rendered.status_code == 201, rendered.text
    assert rendered.json()["content_sha256"] == sha256(body).hexdigest()
    assert rendered.json()["delivery_plan"]["selected_family"] == "image"


@pytest.mark.asyncio
async def test_latex_command_passthrough_preserves_bytes_provenance_and_shared_deletion(
    client, app, tmp_path, monkeypatch
) -> None:
    async def renderer_must_not_run(self, document, *, timeout_seconds):
        del self, document, timeout_seconds
        raise AssertionError("LaTeX tool artifact must not be rendered a second time")

    monkeypatch.setattr(
        DocumentRendererClient,
        "render_document",
        renderer_must_not_run,
    )
    app.state.settings = _render_settings(app, tmp_path)
    await _add_command_instance(app)
    token = "latex-provider-secret"
    descriptor, inventory_hash = await _prepare_tool(
        client,
        app,
        descriptor_path=LATEX_DESCRIPTOR,
        provider_id="provider-latex-primary",
        provider_token=token,
        implementation_hash="6" * 64,
        budget_enforcement={
            "wall_time": "hard",
            "memory": "hard",
            "input_bytes": "hard",
            "output_bytes": "hard",
            "artifact_bytes": "hard",
        },
    )
    tool_input = {"latex": r"x^2+y^2=z^2"}
    invocation, lease = await _invoke_and_start(
        client,
        descriptor=descriptor,
        inventory_hash=inventory_hash,
        provider_token=token,
        tool_input=tool_input,
        key="phase4-latex-command",
    )
    provider_headers = {"Authorization": f"Bearer {token}"}
    body = png_bytes(6, 2)
    digest = sha256(body).hexdigest()
    reserved = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/artifacts/reserve",
        json={
            "schema_version": "1.0",
            "attempt_id": lease["attempt_id"],
            "fencing_token": lease["fencing_token"],
            "lease_secret": lease["lease_secret"],
            "mime_type": "image/png",
            "declared_bytes": len(body),
            "declared_sha256": digest,
        },
        headers={
            **provider_headers,
            "Idempotency-Key": "phase4-latex-artifact",
        },
    )
    assert reserved.status_code == 201, reserved.text
    uploaded = await client.put(
        f"/v1/tool-artifacts/{reserved.json()['artifact_id']}/content",
        content=body,
        headers={
            **provider_headers,
            "Content-Type": "image/png",
            "X-Superlily-Artifact-Upload-Secret": reserved.json()["upload_secret"],
        },
    )
    assert uploaded.status_code == 200, uploaded.text
    finalized = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/artifacts/finalize",
        json={
            "schema_version": "1.0",
            "attempt_id": lease["attempt_id"],
            "fencing_token": lease["fencing_token"],
            "lease_secret": lease["lease_secret"],
            **{
                key: value
                for key, value in uploaded.json().items()
                if key != "state"
            },
        },
        headers=provider_headers,
    )
    assert finalized.status_code == 200, finalized.text
    output = {
        "kind": "image",
        "artifact_id": finalized.json()["artifact_id"],
        "mime_type": "image/png",
        "content_sha256": digest,
        "byte_size": len(body),
        "width_pixels": 6,
        "height_pixels": 2,
    }
    completed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            "schema_version": "1.0",
            "attempt_id": lease["attempt_id"],
            "fencing_token": lease["fencing_token"],
            "lease_secret": lease["lease_secret"],
            "provider_result_id": "phase4-latex-result",
            "output": output,
            "usage": _usage(tool_input, output, artifact_bytes=len(body)),
            "artifacts": [finalized.json()],
        },
        headers=provider_headers,
    )
    assert completed.status_code == 200, completed.text
    rendered = await client.post(
        f"/v1/tool-invocations/{invocation['invocation_id']}/render-result",
        json={
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "conversation_key": "onebot_v11-group_1080353942",
            "source_event_id": "qq:message:phase4-latex-command",
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-latex-render",
        },
    )
    assert rendered.status_code == 201, rendered.text
    receipt = rendered.json()
    assert receipt["content_sha256"] == digest
    downloaded = await client.get(
        receipt["content_path"],
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert downloaded.content == body
    async with app.state.database.sessions() as session:
        source = await session.get(ToolArtifact, output["artifact_id"])
        derived = await session.get(RenderArtifactRecord, receipt["artifact_id"])
    assert source is not None and derived is not None
    assert derived.storage_key == source.storage_key
    assert derived.producer_kind == "tool_artifact_passthrough"
    assert derived.source_invocation_id == invocation["invocation_id"]

    deleted = await client.request(
        "DELETE",
        receipt["content_path"],
        json={"instance_id": "lily-command", "reason": "test_cleanup"},
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert deleted.status_code == 200
    assert deleted.json()["physical_object_removed"] is False
    assert (
        tmp_path
        / "phase4-tool-artifacts"
        / source.storage_key
    ).is_file()
