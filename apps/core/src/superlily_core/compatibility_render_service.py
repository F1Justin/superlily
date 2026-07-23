"""Core-owned Phase 4 compatibility path from tool results to delivery plans."""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    CompatibilityRenderingError,
    HelpDocumentIn,
    MarkdownDocumentIn,
    MarkdownRenderingError,
    ToolRegistryContractError,
    ToolResultRenderIn,
    canonicalize_json_value,
    markdown_to_render_document,
    render_help_document,
    render_tool_result_document,
    validate_schema_instance,
)

from .models import BotInstance, ToolArtifact, ToolAttempt, ToolInvocation
from .render_service import (
    RenderServiceError,
    submit_passthrough_render_document,
    submit_render_document,
)
from .settings import Settings


def _expected_conversation_key(
    instance: BotInstance,
    invocation: ToolInvocation,
) -> str:
    principal = invocation.principal_snapshot_json.get("facts")
    if not isinstance(principal, dict):
        raise RenderServiceError(
            "invocation_principal_invalid",
            "tool invocation principal cannot be mapped to a render conversation",
        )
    conversation_id = principal.get("conversation_id")
    conversation_type = principal.get("conversation_type")
    if (
        not isinstance(conversation_id, str)
        or not isinstance(conversation_type, str)
        or not conversation_id.startswith(f"{conversation_type}:")
    ):
        raise RenderServiceError(
            "invocation_principal_invalid",
            "tool invocation principal cannot be mapped to a render conversation",
        )
    native_id = conversation_id.split(":", 1)[1]
    return f"{instance.adapter}-{conversation_type}_{native_id}"


async def submit_tool_result_render(
    session: AsyncSession,
    settings: Settings,
    invocation_id: str,
    authenticated_instance: str,
    payload: ToolResultRenderIn,
    idempotency_key: str,
):
    if payload.instance_id != authenticated_instance:
        raise RenderServiceError(
            "delivery_forbidden", "render identity does not match token"
        )
    invocation = await session.get(ToolInvocation, invocation_id)
    if (
        invocation is None
        or invocation.creator_type != "command"
        or invocation.creator_id != authenticated_instance
    ):
        raise RenderServiceError(
            "tool_invocation_not_found", "tool invocation was not found"
        )
    if invocation.state != "succeeded":
        raise RenderServiceError(
            "tool_result_unavailable", "tool invocation has no successful result"
        )
    instance = await session.get(BotInstance, authenticated_instance)
    if instance is None:
        raise RenderServiceError("instance_not_found", "render instance was not found")
    if payload.conversation_key != _expected_conversation_key(instance, invocation):
        raise RenderServiceError(
            "conversation_mismatch",
            "tool invocation and render conversation do not match",
        )
    principal_facts = invocation.principal_snapshot_json.get("facts", {})
    principal_source = (
        principal_facts.get("source_event_id")
        if isinstance(principal_facts, dict)
        else None
    )
    if (
        principal_source is not None
        and payload.source_event_id is not None
        and principal_source != payload.source_event_id
    ):
        raise RenderServiceError(
            "source_event_mismatch",
            "tool invocation and render source event do not match",
        )

    attempt = await session.scalar(
        select(ToolAttempt)
        .where(
            ToolAttempt.invocation_id == invocation.id,
            ToolAttempt.state == "succeeded",
        )
        .order_by(desc(ToolAttempt.attempt_number))
        .limit(1)
    )
    if attempt is None or attempt.output_json is None or attempt.output_hash is None:
        raise RenderServiceError(
            "tool_result_unavailable", "successful tool output is unavailable"
        )
    if canonicalize_json_value(attempt.output_json).sha256 != attempt.output_hash:
        raise RenderServiceError(
            "tool_result_integrity_failure", "tool output hash does not match"
        )
    try:
        validate_schema_instance(
            attempt.output_json,
            invocation.descriptor_snapshot_json["output_schema"],
        )
        document = render_tool_result_document(
            payload,
            tool_id=invocation.tool_id,
            descriptor_version=invocation.descriptor_version,
            tool_input=invocation.input_json,
            output=attempt.output_json,
        )
    except (KeyError, ToolRegistryContractError, CompatibilityRenderingError) as exc:
        code = (
            exc.code
            if isinstance(exc, CompatibilityRenderingError)
            else "invalid_tool_result"
        )
        raise RenderServiceError(
            code, "tool result failed the reviewed render contract"
        ) from exc

    if invocation.tool_id != "latex.render":
        return await submit_render_document(
            session,
            settings,
            document,
            idempotency_key,
        )

    artifact_id = attempt.output_json["artifact_id"]
    source_artifact = await session.get(ToolArtifact, artifact_id)
    expected = {
        "artifact_id": source_artifact.id if source_artifact else None,
        "mime_type": source_artifact.mime_type if source_artifact else None,
        "content_sha256": source_artifact.content_sha256 if source_artifact else None,
        "byte_size": source_artifact.byte_size if source_artifact else None,
        "width_pixels": source_artifact.width_pixels if source_artifact else None,
        "height_pixels": source_artifact.height_pixels if source_artifact else None,
    }
    actual = {
        key: attempt.output_json[key]
        for key in (
            "artifact_id",
            "mime_type",
            "content_sha256",
            "byte_size",
            "width_pixels",
            "height_pixels",
        )
    }
    if (
        source_artifact is None
        or source_artifact.invocation_id != invocation.id
        or source_artifact.attempt_id != attempt.id
        or source_artifact.producer_tool_id != invocation.tool_id
        or source_artifact.producer_descriptor_version
        != invocation.descriptor_version
        or expected != actual
    ):
        raise RenderServiceError(
            "source_artifact_binding_mismatch",
            "tool artifact does not match the successful result",
        )
    return await submit_passthrough_render_document(
        session,
        settings,
        document,
        idempotency_key,
        source_artifact=source_artifact,
        source_invocation_id=invocation.id,
        producer_id=f"{invocation.tool_id}@{invocation.descriptor_version}",
    )


async def submit_help_render(
    session: AsyncSession,
    settings: Settings,
    authenticated_instance: str,
    payload: HelpDocumentIn,
    idempotency_key: str,
):
    if payload.instance_id != authenticated_instance:
        raise RenderServiceError(
            "delivery_forbidden", "render identity does not match token"
        )
    return await submit_render_document(
        session,
        settings,
        render_help_document(payload),
        idempotency_key,
    )


async def submit_markdown_render(
    session: AsyncSession,
    settings: Settings,
    authenticated_instance: str,
    payload: MarkdownDocumentIn,
    idempotency_key: str,
):
    """Convert inert Markdown to RenderDocument before the normal render path."""

    if payload.instance_id != authenticated_instance:
        raise RenderServiceError(
            "delivery_forbidden", "render identity does not match token"
        )
    try:
        document = markdown_to_render_document(payload)
    except MarkdownRenderingError as exc:
        raise RenderServiceError(exc.code, exc.safe_detail) from exc
    return await submit_render_document(
        session,
        settings,
        document,
        idempotency_key,
    )
