from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from superlily_contracts import (
    CompatibilityRenderingError,
    HelpDocumentIn,
    ToolResultRenderIn,
    render_help_document,
    render_tool_result_document,
)
from superlily_core.document_renderer_client import (
    DocumentRendererClient,
    RenderedDocument,
)
from superlily_core.models import (
    BotInstance,
    RenderDeliveryAttempt,
    RenderDeliveryIntent,
    RenderDocumentRecord,
    utc_now,
)

from test_artifact_store import png_bytes
from test_tool_execution_api import (
    EXECUTABLE_DESCRIPTOR_PATH,
    PROVIDER_HEADERS,
    completion_usage,
    invocation_payload,
    prepare_canary,
    proof,
    pull_lease,
    successful_output,
)


def test_migrated_result_converters_are_explicit_and_help_is_structured() -> None:
    request = ToolResultRenderIn(
        instance_id="lily-command",
        conversation_key="onebot_v11-group_1080353942",
        source_event_id="qq:message:42",
    )
    status_output = {
        "status": "ok",
        "checked_at": "2026-07-23T12:00:00Z",
        "scope": "provider_runtime",
        "provider_id": "provider-status-primary",
        "descriptor_hash": "a" * 64,
        "implementation_hash": "b" * 64,
    }
    status = render_tool_result_document(
        request,
        tool_id="status.inspect",
        descriptor_version="1.0.2",
        tool_input={"scope": "provider_runtime"},
        output=status_output,
    )
    wolfram = render_tool_result_document(
        request,
        tool_id="wolfram.run",
        descriptor_version="1.0.0",
        tool_input={"expression": "Factor[x^2-1]"},
        output={"kind": "text", "text": "(-1 + x) (1 + x)"},
    )
    latex = render_tool_result_document(
        request,
        tool_id="latex.render",
        descriptor_version="1.0.0",
        tool_input={"latex": r"x^2+y^2=z^2"},
        output={
            "kind": "image",
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "mime_type": "image/png",
            "content_sha256": "c" * 64,
            "byte_size": 100,
            "width_pixels": 100,
            "height_pixels": 40,
        },
    )
    help_document = render_help_document(
        HelpDocumentIn(
            instance_id="lily-command",
            conversation_key="onebot_v11-group_1080353942",
            commands=[
                {"name": "/status", "summary": "查看状态"},
                {
                    "name": "/wolfram",
                    "summary": "执行受限计算",
                    "usage": "/wolfram <表达式>",
                },
            ],
        )
    )

    assert status.blocks[0].kind == "card"
    assert wolfram.blocks[0].kind == "code"
    assert wolfram.blocks[0].code == "(-1 + x) (1 + x)"
    assert latex.blocks[0].kind == "image"
    assert latex.blocks[0].accessibility_text == r"LaTeX 公式：x^2+y^2=z^2"
    assert help_document.blocks[0].kind == "list"
    assert help_document.blocks[0].items[0].startswith("**/status**")
    with pytest.raises(CompatibilityRenderingError, match="reviewed Phase 4"):
        render_tool_result_document(
            request,
            tool_id="unknown.run",
            descriptor_version="1.0.0",
            tool_input={},
            output={},
        )


@pytest.mark.asyncio
async def test_status_and_help_commands_share_core_render_and_delivery_plan(
    client, app, tmp_path, monkeypatch
) -> None:
    body = png_bytes(4, 3)

    async def render_document(self, document, *, timeout_seconds):
        del self, document, timeout_seconds
        return RenderedDocument(
            content=body,
            content_sha256=sha256(body).hexdigest(),
            width_pixels=4,
            height_pixels=3,
        )

    monkeypatch.setattr(DocumentRendererClient, "render_document", render_document)
    app.state.settings = replace(
        app.state.settings,
        artifact_root=str(tmp_path / "compat-artifacts"),
        artifact_secret_pepper="compatibility-test-pepper-0123456789",
        render_mode="canary",
        render_canary_conversations=frozenset(
            {"onebot_v11-group_1080353942"}
        ),
        render_backend_url="http://document-renderer:8000",
        render_backend_token="r" * 32,
        render_implementation_hash="9" * 64,
    )
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

    descriptor, inventory_hash = await prepare_canary(
        client,
        app,
        descriptor_path=EXECUTABLE_DESCRIPTOR_PATH,
        caller="command",
    )
    invocation_response = await client.post(
        "/v1/tool-invocations",
        json=invocation_payload(
            descriptor.descriptor_hash,
            descriptor_version=descriptor.version,
        ),
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-status-command-invocation",
        },
    )
    assert invocation_response.status_code == 201, invocation_response.text
    invocation = invocation_response.json()
    assert invocation["state"] == "queued"
    lease_response = await pull_lease(client, inventory_hash)
    assert lease_response.status_code == 200, lease_response.text
    lease = lease_response.json()
    started = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/start",
        json=proof(lease),
        headers=PROVIDER_HEADERS,
    )
    assert started.status_code == 200
    output = successful_output(descriptor.descriptor_hash)
    completed = await client.post(
        f"/v1/tool-executions/{invocation['invocation_id']}/complete",
        json={
            **proof(lease),
            "provider_result_id": "phase4-status-result",
            "output": output,
            "usage": completion_usage(output),
        },
        headers=PROVIDER_HEADERS,
    )
    assert completed.status_code == 200, completed.text

    rendered = await client.post(
        f"/v1/tool-invocations/{invocation['invocation_id']}/render-result",
        json={
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "conversation_key": "onebot_v11-group_1080353942",
            "source_event_id": "qq:message:attempt-canary-1",
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-status-render",
        },
    )
    assert rendered.status_code == 201, rendered.text
    status_receipt = rendered.json()
    assert status_receipt["delivery_plan"]["selected_family"] == "image"
    assert status_receipt["delivery_plan"]["ordered_payloads"][0]["family"] == "image"

    help_rendered = await client.post(
        "/v1/help-documents",
        json={
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "conversation_key": "onebot_v11-group_1080353942",
            "source_event_id": "qq:message:help-1",
            "commands": [
                {"name": "/status", "summary": "查看状态"},
                {
                    "name": "/wolfram",
                    "summary": "受限计算",
                    "usage": "/wolfram <表达式>",
                },
            ],
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-help-render",
        },
    )
    assert help_rendered.status_code == 201, help_rendered.text
    assert help_rendered.json()["delivery_plan"]["selected_family"] == "image"

    markdown_rendered = await client.post(
        "/v1/markdown-documents",
        json={
            "schema_version": "1.0",
            "instance_id": "lily-command",
            "conversation_key": "onebot_v11-group_1080353942",
            "source_event_id": "qq:message:markdown-1",
            "markdown": (
                "# 普通 Markdown\n\n"
                "段落里直接写 $x^2$ 和 **加粗**。\n\n"
                "- **第一项：** 不需要 blocks JSON"
            ),
        },
        headers={
            "Authorization": "Bearer lily-secret",
            "Idempotency-Key": "phase4-markdown-render",
        },
    )
    assert markdown_rendered.status_code == 201, markdown_rendered.text
    assert markdown_rendered.json()["delivery_plan"]["selected_family"] == "image"
    expiring_intent = await client.post(
        (
            f"/v1/render-artifacts/{markdown_rendered.json()['artifact_id']}"
            "/delivery-intents"
        ),
        json={
            "instance_id": "lily-command",
            "delivery_plan_id": markdown_rendered.json()["delivery_plan_id"],
            "idempotency_key": "phase4-expiring-intent",
        },
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert expiring_intent.status_code == 201
    async with app.state.database.sessions() as session:
        intent_row = await session.get(
            RenderDeliveryIntent,
            expiring_intent.json()["intent_id"],
        )
        assert intent_row is not None
        intent_row.deadline_at = utc_now() - timedelta(seconds=1)
        await session.commit()
    expired_replay = await client.post(
        (
            f"/v1/render-artifacts/{markdown_rendered.json()['artifact_id']}"
            "/delivery-intents"
        ),
        json={
            "instance_id": "lily-command",
            "delivery_plan_id": markdown_rendered.json()["delivery_plan_id"],
            "idempotency_key": "phase4-expiring-intent",
        },
        headers={"Authorization": "Bearer lily-secret"},
    )
    assert expired_replay.status_code == 200
    assert expired_replay.json()["should_send"] is False
    assert expired_replay.json()["status"] == "ambiguous"

    async with app.state.database.sessions() as session:
        status_document = await session.get(
            RenderDocumentRecord, status_receipt["render_id"]
        )
        help_document = await session.get(
            RenderDocumentRecord, help_rendered.json()["render_id"]
        )
        markdown_document = await session.get(
            RenderDocumentRecord, markdown_rendered.json()["render_id"]
        )
        expired_attempt = await session.scalar(
            select(RenderDeliveryAttempt).where(
                RenderDeliveryAttempt.intent_id == expiring_intent.json()["intent_id"]
            )
        )
    assert status_document is not None
    assert status_document.document_json["blocks"][0]["kind"] == "card"
    assert help_document is not None
    assert help_document.document_json["blocks"][0]["kind"] == "list"
    assert markdown_document is not None
    assert [block["kind"] for block in markdown_document.document_json["blocks"]] == [
        "heading",
        "paragraph",
        "list",
    ]
    assert expired_attempt is not None
    assert expired_attempt.outcome == "ambiguous"
    assert expired_attempt.safe_error_code == "platform_completion_unknown"
