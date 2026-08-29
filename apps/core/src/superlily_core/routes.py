from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import (
    AgentAttemptReportIn,
    AgentRunCreateIn,
    AgentTextDeliveryCompleteIn,
    AgentTextDeliveryLeaseIn,
    AgentToolPromotionIn,
    CommandRegistrySnapshotIn,
    DeliveryCompletionIn,
    DeliveryIntentIn,
    EventIn,
    HeartbeatIn,
    HelpDocumentIn,
    MarkdownDocumentIn,
    ProviderHeartbeatIn,
    ProviderInventorySnapshotIn,
    ResponseIn,
    DeliveryAttemptIn,
    RenderArtifactDeletionIn,
    RenderDocument,
    ToolInvocationCancelIn,
    ToolInvocationConfirmIn,
    ToolInvocationCreateIn,
    ToolResultRenderIn,
    ToolArtifactFinalizeIn,
    ToolArtifactReserveIn,
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolLeaseRequestIn,
)

from .audit import classify_decision_outcome
from .auth import (
    InvocationIdentity,
    ingest_identity,
    invocation_identity,
    model_provider_identity,
    provider_identity,
    require_admin,
)
from .command_registry import (
    load_command_registry,
    runtime_candidate_trigger_reviewed,
    runtime_plugin_aliases,
    source_plugin_loaded,
)
from .dependencies import get_session
from .models import (
    BotInstance,
    CollectorWatermark,
    CommandRegistrySnapshot,
    EventClaim,
    EventDecision,
    EventLink,
    EventObservation,
    AgentInteraction,
    AgentToolLoop,
    IngressReceiptRecord,
    ResponseRecord,
    SourceEvent,
)
from .service import (
    acknowledge_event_claim,
    claim_record_payload,
    effective_status,
    evaluate_event_claim,
    ingest_command_registry_snapshot,
    ingest_event,
    ingest_heartbeat,
    ingest_response,
    ingress_receipt_view,
    resolve_pending_links,
)
from .render_service import (
    RenderServiceError,
    complete_delivery_intent,
    create_delivery_intent,
    delete_render_artifact_content,
    get_render_artifact,
    record_delivery_attempt,
    submit_render_document,
)
from .compatibility_render_service import (
    submit_help_render,
    submit_markdown_render,
    submit_tool_result_render,
)
from .tool_registry_service import (
    ingest_provider_heartbeat,
    ingest_provider_inventory,
    tool_registry_view,
)
from .tool_invocation_service import (
    cancel_tool_invocation,
    create_tool_invocation,
    decide_tool_confirmation,
    get_tool_invocation,
    invocation_view,
)
from .tool_execution_service import (
    attempt_views,
    complete_tool_execution,
    fail_tool_execution,
    heartbeat_tool_execution,
    lease_tool_execution,
    start_tool_execution,
)
from .tool_artifact_service import (
    finalize_tool_artifact,
    reserve_tool_artifact,
    upload_tool_artifact,
)
from .agent_run_service import (
    agent_run_view,
    create_agent_run,
    get_agent_run_for_admin,
    planner_input_for_provider,
    record_agent_attempt,
)
from .agent_tool_loop_service import (
    agent_tool_loop_view,
    continuation_input,
    promote_wolfram_proposal,
    record_continuation,
)
from .agent_product_service import (
    accept_agent_interaction,
    complete_agent_delivery,
    interaction_view,
    lease_agent_delivery,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_session)]
Identity = Annotated[str, Depends(ingest_identity)]
ProviderIdentity = Annotated[str, Depends(provider_identity)]
ModelProviderIdentity = Annotated[str, Depends(model_provider_identity)]
ToolInvocationIdentity = Annotated[InvocationIdentity, Depends(invocation_identity)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=256)]
_ARTIFACT_UPLOAD_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def _verify_identity(authenticated_instance: str, payload_instance: str) -> None:
    if authenticated_instance != payload_instance:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="token is not authorized for payload instance",
        )


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    try:
        await request.app.state.database.ping()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="database unavailable") from exc
    return {"status": "ok", "database": "ok"}


@router.post("/v1/events", status_code=status.HTTP_201_CREATED)
async def post_event(
    payload: EventIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate = await ingest_event(session, payload, idempotency_key, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return await ingress_receipt_view(session, record, duplicate=duplicate)


@router.post("/v1/claims/evaluate", status_code=status.HTTP_200_OK)
async def post_claim(
    payload: EventIn,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate, observation, event_duplicate = await evaluate_event_claim(
        session,
        payload,
        idempotency_key,
        session.info["settings"],
    )
    receipt = await ingress_receipt_view(
        session,
        observation,
        duplicate=event_duplicate,
    )
    return {
        **claim_record_payload(record),
        "duplicate": duplicate,
        "ingest_receipt": receipt,
    }


@router.post("/v1/claims/{claim_id}/ack", status_code=status.HTTP_200_OK)
async def post_claim_ack(
    claim_id: str,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    del idempotency_key  # Presence is required to make bridge retries explicit.
    record, duplicate = await acknowledge_event_claim(
        session,
        claim_id,
        authenticated_instance,
    )
    return {**claim_record_payload(record), "duplicate": duplicate}


@router.post("/v1/responses", status_code=status.HTTP_201_CREATED)
async def post_response(
    payload: ResponseIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record, duplicate = await ingest_response(session, payload, idempotency_key, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {"response_id": record.id, "source_response_id": record.source_response_id, "duplicate": duplicate}


def _render_http_error(exc: RenderServiceError) -> HTTPException:
    if exc.code.startswith("markdown_") or exc.code == "renderer_content_error":
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif exc.code in {"render_conversation_forbidden", "artifact_forbidden", "delivery_forbidden"}:
        code = status.HTTP_403_FORBIDDEN
    elif exc.code in {
        "artifact_not_found",
        "delivery_intent_not_found",
        "delivery_plan_not_found",
        "tool_invocation_not_found",
    }:
        code = status.HTTP_404_NOT_FOUND
    elif exc.code in {
        "delivery_completion_conflict",
        "idempotency_conflict",
        "conversation_mismatch",
        "source_event_mismatch",
        "tool_result_unavailable",
    }:
        code = status.HTTP_409_CONFLICT
    elif exc.code == "artifact_deleted":
        code = status.HTTP_410_GONE
    elif exc.code in {"render_disabled", "render_in_progress", "artifact_expired"}:
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        code = status.HTTP_502_BAD_GATEWAY
    detail: str | dict[str, object] = exc.safe_detail
    if exc.diagnostic is not None:
        detail = {
            "message": exc.safe_detail,
            "diagnostic": exc.diagnostic.model_dump(mode="json"),
        }
    return HTTPException(
        status_code=code,
        detail=detail,
        headers={"X-Render-Error-Code": exc.code},
    )


def _render_receipt(record, attempt, artifact, plan, duplicate: bool) -> dict:
    return {
        "render_id": record.id,
        "artifact_id": artifact.id,
        "attempt_id": attempt.id,
        "delivery_plan_id": plan.id,
        "request_sha256": record.request_sha256,
        "content_sha256": artifact.content_sha256,
        "mime_type": artifact.mime_type,
        "byte_size": artifact.byte_size,
        "width_pixels": artifact.width_pixels,
        "height_pixels": artifact.height_pixels,
        "render_duration_ms": record.render_duration_ms or 0,
        "content_path": f"/v1/render-artifacts/{artifact.id}/content",
        "delivery_plan": {
            "delivery_plan_id": plan.id,
            "capability_hash": plan.capability_hash,
            "selected_family": plan.selected_family,
            "fallback_text": plan.fallback_text,
            "degradation_reasons": plan.degradation_reasons_json,
            "decision_hash": plan.decision_hash,
            "resolved_document_hash": plan.resolved_document_hash,
            "selected_alternatives": plan.selected_alternatives_json,
            "rejected_alternatives": plan.rejected_alternatives_json,
            "ordered_payloads": plan.ordered_payloads_json,
        },
        "duplicate": duplicate,
    }


@router.post("/v1/render-documents", status_code=status.HTTP_201_CREATED)
async def post_render_document(
    payload: RenderDocument,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    _verify_identity(authenticated_instance, payload.instance_id)
    try:
        record, attempt, artifact, plan, duplicate = await submit_render_document(
            session,
            session.info["settings"],
            payload,
            idempotency_key,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return _render_receipt(record, attempt, artifact, plan, duplicate)


@router.post("/v1/markdown-documents", status_code=status.HTTP_201_CREATED)
async def post_markdown_document(
    payload: MarkdownDocumentIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    try:
        record, attempt, artifact, plan, duplicate = await submit_markdown_render(
            session,
            session.info["settings"],
            authenticated_instance,
            payload,
            idempotency_key,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return _render_receipt(record, attempt, artifact, plan, duplicate)


@router.post(
    "/v1/tool-invocations/{invocation_id}/render-result",
    status_code=status.HTTP_201_CREATED,
)
async def post_tool_result_render(
    invocation_id: str,
    payload: ToolResultRenderIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    try:
        record, attempt, artifact, plan, duplicate = await submit_tool_result_render(
            session,
            session.info["settings"],
            invocation_id,
            authenticated_instance,
            payload,
            idempotency_key,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return _render_receipt(record, attempt, artifact, plan, duplicate)


@router.post("/v1/help-documents", status_code=status.HTTP_201_CREATED)
async def post_help_document(
    payload: HelpDocumentIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    try:
        record, attempt, artifact, plan, duplicate = await submit_help_render(
            session,
            session.info["settings"],
            authenticated_instance,
            payload,
            idempotency_key,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return _render_receipt(record, attempt, artifact, plan, duplicate)


@router.get("/v1/render-artifacts/{artifact_id}/content")
async def get_render_artifact_content(
    artifact_id: str,
    session: Session,
    authenticated_instance: Identity,
) -> Response:
    try:
        artifact, content = await get_render_artifact(
            session,
            session.info["settings"],
            artifact_id,
            authenticated_instance,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    return Response(
        content=content,
        media_type="image/png",
        headers={
            "Cache-Control": "private, no-store",
            "Content-SHA256": artifact.content_sha256,
        },
    )


@router.delete("/v1/render-artifacts/{artifact_id}/content")
async def delete_render_artifact(
    artifact_id: str,
    payload: RenderArtifactDeletionIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict:
    try:
        artifact, physical_removed, duplicate = await delete_render_artifact_content(
            session,
            session.info["settings"],
            artifact_id,
            authenticated_instance,
            payload,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    return {
        "artifact_id": artifact.id,
        "content_deleted": artifact.content_deleted_at is not None,
        "physical_object_removed": physical_removed,
        "duplicate": duplicate,
    }


@router.post("/v1/render-artifacts/{artifact_id}/delivery-attempts", status_code=201)
async def post_render_delivery_attempt(
    artifact_id: str,
    payload: DeliveryAttemptIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str]:
    try:
        attempt = await record_delivery_attempt(
            session,
            artifact_id,
            authenticated_instance,
            payload,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    return {"attempt_id": attempt.id, "outcome": attempt.outcome}


@router.post("/v1/render-artifacts/{artifact_id}/delivery-intents", status_code=201)
async def post_render_delivery_intent(
    artifact_id: str,
    payload: DeliveryIntentIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str | bool]:
    try:
        intent, should_send = await create_delivery_intent(
            session,
            session.info["settings"],
            artifact_id,
            authenticated_instance,
            payload,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    if not should_send:
        response.status_code = status.HTTP_200_OK
    return {
        "intent_id": intent.id,
        "should_send": should_send,
        "status": intent.status,
        "duplicate": not should_send,
    }


@router.post("/v1/render-delivery-intents/{intent_id}/complete")
async def post_render_delivery_completion(
    intent_id: str,
    payload: DeliveryCompletionIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str | bool]:
    try:
        intent, attempt, duplicate = await complete_delivery_intent(
            session,
            intent_id,
            authenticated_instance,
            payload,
        )
    except RenderServiceError as exc:
        raise _render_http_error(exc) from exc
    return {
        "intent_id": intent.id,
        "attempt_id": attempt.id,
        "outcome": intent.status,
        "duplicate": duplicate,
    }


@router.post("/v1/heartbeats", status_code=status.HTTP_200_OK)
async def post_heartbeat(
    payload: HeartbeatIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    record = await ingest_heartbeat(session, payload, session.info["settings"])
    return {"instance_id": record.id, "reported_status": record.reported_status}


@router.post("/v1/command-registry/snapshots", status_code=status.HTTP_201_CREATED)
async def post_command_registry_snapshot(
    payload: CommandRegistrySnapshotIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    if authenticated_instance != "lily-command":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only lily-command may publish the runtime command registry",
        )
    record, duplicate = await ingest_command_registry_snapshot(session, payload, session.info["settings"])
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {"snapshot_id": record.id, "snapshot_hash": record.snapshot_hash, "duplicate": duplicate}


@router.post("/v1/provider-inventory/snapshots", status_code=status.HTTP_201_CREATED)
async def post_provider_inventory_snapshot(
    payload: ProviderInventorySnapshotIn,
    response: Response,
    session: Session,
    authenticated_provider: ProviderIdentity,
    idempotency_key: IdempotencyKey,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_provider, payload.provider_id)
    record, duplicate = await ingest_provider_inventory(session, payload, idempotency_key)
    if duplicate:
        response.status_code = status.HTTP_200_OK
    return {
        "provider_id": record.provider_id,
        "snapshot_id": record.id,
        "snapshot_hash": record.snapshot_hash,
        "duplicate": duplicate,
    }


@router.post("/v1/providers/heartbeats", status_code=status.HTTP_200_OK)
async def post_provider_heartbeat(
    payload: ProviderHeartbeatIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict[str, str | bool]:
    _verify_identity(authenticated_provider, payload.provider_id)
    record, duplicate = await ingest_provider_heartbeat(session, payload)
    return {
        "provider_id": record.provider_id,
        "heartbeat_id": record.id,
        "inventory_hash": record.inventory_hash,
        "duplicate": duplicate,
    }


@router.get("/v1/events/recent", dependencies=[Depends(require_admin)])
async def recent_events(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventObservation, SourceEvent, IngressReceiptRecord)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .outerjoin(
                IngressReceiptRecord,
                IngressReceiptRecord.observation_id == EventObservation.id,
            )
            .order_by(desc(EventObservation.received_at))
            .limit(limit)
        )
    ).all()
    return [
        {
            "observation_id": observation.id,
            "source_event_id": source.id,
            "reported_source_event_id": observation.reported_source_event_id,
            "instance_id": observation.instance_id,
            "platform": source.platform,
            "adapter": observation.adapter,
            "event_type": source.event_type,
            "conversation": {
                "id": source.conversation_id,
                "type": source.conversation_type,
                "name": observation.conversation_name,
            },
            "sender": {"id": observation.sender_id, "name": observation.sender_name},
            "message_id": observation.platform_message_id,
            "native_identity": observation.metadata_json.get("native_identity"),
            "correlation_diagnostic": observation.metadata_json.get("correlation"),
            "correlation_version": source.correlation_version,
            "capture": {
                "profile": observation.capture_profile,
                "policy_version": observation.capture_policy_version,
                "status": observation.capture_status,
                "sanitizer_version": observation.sanitizer_version,
                "collector_sanitizer_version": observation.collector_sanitizer_version,
                "original_payload_sha256": observation.original_payload_sha256,
                "original_payload_size_bytes": observation.original_payload_size_bytes,
                "omitted_fields": observation.omitted_fields_json,
                "reason": observation.capture_reason,
            },
            "ingress": (
                None
                if receipt is None
                else {
                    "spool_id": receipt.spool_id,
                    "sequence": receipt.collector_sequence,
                    "record_sha256": receipt.record_sha256,
                    "captured_at": receipt.captured_at,
                    "committed_at": receipt.committed_at,
                }
            ),
            "text": observation.text,
            "occurred_at": source.occurred_at,
            "received_at": observation.received_at,
        }
        for observation, source, receipt in rows
    ]


@router.get("/v1/ingress/watermarks", dependencies=[Depends(require_admin)])
async def ingress_watermarks(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.scalars(
            select(CollectorWatermark)
            .order_by(desc(CollectorWatermark.updated_at))
            .limit(limit)
        )
    ).all()
    return [
        {
            "schema_version": "1.0",
            "instance_id": item.instance_id,
            "spool_id": item.spool_id,
            "highest_contiguous_sequence": item.highest_contiguous_sequence,
            "highest_seen_sequence": item.highest_seen_sequence,
            "next_gap_sequence": (
                item.highest_contiguous_sequence + 1
                if item.highest_seen_sequence > item.highest_contiguous_sequence
                else None
            ),
            "last_receipt_at": item.last_receipt_at,
            "updated_at": item.updated_at,
        }
        for item in rows
    ]


@router.get("/v1/ingress/status", dependencies=[Depends(require_admin)])
async def ingress_status(request: Request, session: Session) -> list[dict]:
    instances = (await session.scalars(select(BotInstance).order_by(BotInstance.id))).all()
    watermarks = (await session.scalars(select(CollectorWatermark))).all()
    watermark_by_scope = {
        (item.instance_id, item.spool_id): item for item in watermarks
    }
    result: list[dict] = []
    for item in instances:
        spool = item.metadata_json.get("ingress_spool")
        spool = spool if isinstance(spool, dict) else None
        spool_id = spool.get("spool_id") if spool is not None else None
        watermark = (
            watermark_by_scope.get((item.id, spool_id))
            if isinstance(spool_id, str) and spool_id
            else None
        )
        highest_sequence = (
            int(spool.get("highest_sequence", 0)) if spool is not None else 0
        )
        highest_contiguous = (
            watermark.highest_contiguous_sequence if watermark is not None else 0
        )
        highest_seen = watermark.highest_seen_sequence if watermark is not None else 0
        lag_records = max(0, highest_sequence - highest_contiguous)
        collector_status = effective_status(
            item,
            request.app.state.settings.stale_after_seconds,
        )
        if spool is None:
            reconciliation_state = "unknown"
        elif collector_status == "offline":
            reconciliation_state = "stale"
        elif spool.get("state") in {"error", "quarantined", "quota_pressure"}:
            reconciliation_state = str(spool["state"])
        elif lag_records > 0 or int(spool.get("pending_records", 0)) > 0:
            reconciliation_state = "pending"
        else:
            reconciliation_state = "reconciled"
        result.append(
            {
                "schema_version": "1.0",
                "instance_id": item.id,
                "collector_status": collector_status,
                "last_heartbeat_at": item.last_heartbeat_at,
                "spool": spool,
                "core_watermark": (
                    None
                    if watermark is None
                    else {
                        "spool_id": watermark.spool_id,
                        "highest_contiguous_sequence": highest_contiguous,
                        "highest_seen_sequence": highest_seen,
                        "next_gap_sequence": (
                            highest_contiguous + 1
                            if highest_seen > highest_contiguous
                            else None
                        ),
                        "last_receipt_at": watermark.last_receipt_at,
                    }
                ),
                "reconciliation": {
                    "state": reconciliation_state,
                    "lag_records": lag_records,
                    "next_gap_sequence": (
                        highest_contiguous + 1
                        if highest_seen > highest_contiguous
                        else None
                    ),
                },
            }
        )
    return result


@router.get("/v1/responses/recent", dependencies=[Depends(require_admin)])
async def recent_responses(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.scalars(select(ResponseRecord).order_by(desc(ResponseRecord.received_at)).limit(limit))
    ).all()
    return [
        {
            "response_id": item.id,
            "source_response_id": item.source_response_id,
            "instance_id": item.instance_id,
            "trigger_observation_id": item.trigger_observation_id,
            "trigger_source_event_id": item.trigger_source_event_id,
            "conversation_id": item.conversation_id,
            "platform_message_id": item.platform_message_id,
            "response_type": item.response_type,
            "text": item.text,
            "success": item.success,
            "error": item.error,
            "latency_ms": item.latency_ms,
            "metadata": item.metadata_json,
            "occurred_at": item.occurred_at,
            "received_at": item.received_at,
        }
        for item in rows
    ]


@router.get("/v1/event-links/recent", dependencies=[Depends(require_admin)])
async def recent_event_links(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventLink).order_by(desc(EventLink.created_at)).limit(limit))).all()
    return [
        {
            "link_id": item.id,
            "from_source_event_id": item.from_source_event_id,
            "from_observation_id": item.from_observation_id,
            "to_source_event_id": item.to_source_event_id,
            "relation_type": item.relation_type,
            "target_source_event_id": item.target_source_event_id,
            "target_platform_message_id": item.target_platform_message_id,
            "target_conversation_id": item.target_conversation_id,
            "target_conversation_type": item.target_conversation_type,
            "target_sender_id": item.target_sender_id,
            "confidence": item.confidence,
            "resolver_status": item.resolver_status,
            "created_at": item.created_at,
        }
        for item in rows
    ]


@router.post("/v1/event-links/resolve", dependencies=[Depends(require_admin)])
async def resolve_event_links(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=5000)] = 5000,
) -> dict[str, int]:
    return await resolve_pending_links(session, session.info["settings"], limit)


@router.get("/v1/decisions/recent", dependencies=[Depends(require_admin)])
async def recent_decisions(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventDecision).order_by(desc(EventDecision.updated_at)).limit(limit))).all()
    return [
        {
            "decision_id": item.id,
            "source_event_id": item.source_event_id,
            "deciding_observation_id": item.deciding_observation_id,
            "policy_version": item.policy_version,
            "decision_type": item.decision_type,
            "target_instance_id": item.target_instance_id,
            "confidence": item.confidence,
            "reason": item.reason,
            "features": item.features_json,
            "revision": item.revision,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in rows
    ]


@router.get("/v1/claims/recent", dependencies=[Depends(require_admin)])
async def recent_claims(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (await session.scalars(select(EventClaim).order_by(desc(EventClaim.created_at)).limit(limit))).all()
    return [claim_record_payload(item) for item in rows]


@router.get("/v1/claims/summary", dependencies=[Depends(require_admin)])
async def claim_summary(
    request: Request,
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = (
        await session.scalars(
            select(EventClaim).where(EventClaim.created_at >= since).order_by(EventClaim.created_at)
        )
    ).all()
    return {
        "since": since,
        "hours": hours,
        "mode": request.app.state.settings.claim_mode,
        "canary_conversations": sorted(request.app.state.settings.claim_canary_conversations),
        "claims": len(rows),
        "actions": dict(sorted(Counter(item.action for item in rows).items())),
        "enforced": dict(sorted(Counter(item.action for item in rows if item.enforced).items())),
        "acknowledged": dict(
            sorted(Counter(item.action for item in rows if item.acknowledged_at is not None).items())
        ),
        "reasons": dict(sorted(Counter(item.reason for item in rows).items())),
        "by_instance": dict(sorted(Counter(item.instance_id for item in rows).items())),
    }


def _compact_text(value: str | None, limit: int = 80) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


@router.get("/v1/native-identities/recent", dependencies=[Depends(require_admin)])
async def recent_native_identities(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventObservation, SourceEvent)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .order_by(desc(EventObservation.received_at))
            .limit(min(limit * 5, 2500))
        )
    ).all()
    result = []
    for observation, source in rows:
        native_identity = observation.metadata_json.get("native_identity")
        if not isinstance(native_identity, dict) or not native_identity:
            continue
        text_preview = _compact_text(observation.text)
        conversation = f"{source.conversation_type}:{source.conversation_id}"
        summary = (
            f"{source.occurred_at.isoformat()} | {conversation} | {observation.instance_id} | "
            f"sender={observation.sender_id or '-'} | message_id={native_identity.get('message_id', '-')} | "
            f"real_seq={native_identity.get('real_seq', '-')} | {text_preview}"
        )
        result.append(
            {
                "summary": summary,
                "source_event_id": source.id,
                "observation_id": observation.id,
                "instance_id": observation.instance_id,
                "conversation": {
                    "id": source.conversation_id,
                    "type": source.conversation_type,
                    "display": conversation,
                },
                "sender_id": observation.sender_id,
                "text_preview": text_preview,
                "platform_message_id": observation.platform_message_id,
                "native_identity": native_identity,
                "occurred_at": source.occurred_at,
                "received_at": observation.received_at,
            }
        )
        if len(result) >= limit:
            break
    return result


@router.get("/v1/native-identities/coverage", dependencies=[Depends(require_admin)])
async def native_identity_coverage(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    observations = (
        await session.scalars(
            select(EventObservation)
            .join(SourceEvent, SourceEvent.id == EventObservation.source_event_id)
            .where(EventObservation.received_at >= since)
            .where(SourceEvent.event_type == "message")
            .order_by(desc(EventObservation.received_at))
        )
    ).all()
    by_instance: dict[str, dict] = {}
    for observation in observations:
        stats = by_instance.setdefault(
            observation.instance_id,
            {"observations": 0, "with_native_identity": 0, "fields": {}},
        )
        stats["observations"] += 1
        native_identity = observation.metadata_json.get("native_identity")
        if not isinstance(native_identity, dict) or not native_identity:
            continue
        stats["with_native_identity"] += 1
        for field in native_identity:
            if field == "schema":
                continue
            stats["fields"][field] = stats["fields"].get(field, 0) + 1

    instances = []
    for instance_id, stats in sorted(by_instance.items()):
        total = stats["observations"]
        captured = stats["with_native_identity"]
        instances.append(
            {
                "instance_id": instance_id,
                **stats,
                "coverage_percent": round(captured * 100 / total, 2) if total else 0.0,
            }
        )
    return {"since": since, "hours": hours, "observations": len(observations), "instances": instances}


@router.get("/v1/decisions/summary", dependencies=[Depends(require_admin)])
async def decision_summary(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict]:
    rows = (
        await session.execute(
            select(EventDecision, SourceEvent, EventObservation)
            .join(SourceEvent, SourceEvent.id == EventDecision.source_event_id)
            .join(EventObservation, EventObservation.id == EventDecision.deciding_observation_id, isouter=True)
            .order_by(desc(EventDecision.updated_at))
            .limit(limit)
        )
    ).all()
    result = []
    for decision, source, observation in rows:
        conversation = f"{source.conversation_type}:{source.conversation_id}"
        sender = None
        text = None
        instance_id = None
        if observation is not None:
            sender = observation.sender_name or observation.sender_id
            text = observation.text
            instance_id = observation.instance_id
        text_preview = _compact_text(text or decision.features_json.get("text_preview"))
        target = decision.target_instance_id or "-"
        sender_display = sender or "-"
        summary = (
            f"{decision.created_at.isoformat()} | {conversation} | {sender_display} | "
            f"{text_preview} | {decision.decision_type} -> {target} | {decision.reason}"
        )
        result.append(
            {
                "summary": summary,
                "created_at": decision.created_at,
                "updated_at": decision.updated_at,
                "revision": decision.revision,
                "conversation": {
                    "id": source.conversation_id,
                    "type": source.conversation_type,
                    "display": conversation,
                },
                "sender": sender,
                "text_preview": text_preview,
                "decision_type": decision.decision_type,
                "target_instance_id": decision.target_instance_id,
                "reason": decision.reason,
                "confidence": decision.confidence,
                "source_event_id": decision.source_event_id,
                "observation_id": decision.deciding_observation_id,
                "instance_id": instance_id,
            }
        )
    return result


@router.get("/v1/decisions/outcomes", dependencies=[Depends(require_admin)])
async def decision_outcomes(
    session: Session,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    grace_seconds: Annotated[int, Query(ge=0, le=3600)] = 30,
    detail_limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    decision_rows = (
        await session.execute(
            select(EventDecision, SourceEvent, EventObservation)
            .join(SourceEvent, SourceEvent.id == EventDecision.source_event_id)
            .join(EventObservation, EventObservation.id == EventDecision.deciding_observation_id, isouter=True)
            .where(SourceEvent.first_received_at >= since)
            .order_by(desc(EventDecision.updated_at))
        )
    ).all()
    source_ids = {decision.source_event_id for decision, _, _ in decision_rows}
    responses = (
        await session.scalars(
            select(ResponseRecord)
            .where(ResponseRecord.received_at >= since)
            .order_by(ResponseRecord.received_at)
        )
    ).all()
    responses_by_source: dict[str, list[ResponseRecord]] = defaultdict(list)
    for response in responses:
        if response.trigger_source_event_id:
            responses_by_source[response.trigger_source_event_id].append(response)

    outcome_counts: Counter[str] = Counter()
    details = []
    for decision, source, observation in decision_rows:
        linked = responses_by_source.get(source.id, [])
        successful = [item.instance_id for item in linked if item.success]
        ambiguous = [
            item.instance_id
            for item in linked
            if not item.success and item.metadata_json.get("completion_status") == "ambiguous"
        ]
        suppressed = [
            item.instance_id
            for item in linked
            if not item.success and item.metadata_json.get("completion_status") == "suppressed"
        ]
        failed = [
            item.instance_id
            for item in linked
            if not item.success
            and item.metadata_json.get("completion_status") not in {"ambiguous", "suppressed"}
        ]
        successful_counts = Counter(successful)
        failed_counts = Counter(failed)
        ambiguous_counts = Counter(ambiguous)
        suppressed_counts = Counter(suppressed)
        first_received_at = source.first_received_at
        if first_received_at.tzinfo is None:
            first_received_at = first_received_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - first_received_at).total_seconds()))
        outcome = classify_decision_outcome(
            decision_type=decision.decision_type,
            target_instance_id=decision.target_instance_id,
            successful_instances=successful,
            failed_instances=failed,
            ambiguous_instances=ambiguous,
            age_seconds=age_seconds,
            grace_seconds=grace_seconds,
        )
        outcome_counts[outcome] += 1
        if outcome not in {"matched", "matched_no_response"} and len(details) < detail_limit:
            details.append(
                {
                    "source_event_id": source.id,
                    "conversation": f"{source.conversation_type}:{source.conversation_id}",
                    "sender": observation.sender_name or observation.sender_id if observation else None,
                    "text_preview": _compact_text(observation.text if observation else None),
                    "decision_type": decision.decision_type,
                    "target_instance_id": decision.target_instance_id,
                    "reason": decision.reason,
                    "outcome": outcome,
                    "successful_instances": sorted(successful_counts),
                    "failed_instances": sorted(failed_counts),
                    "successful_response_counts": dict(sorted(successful_counts.items())),
                    "failed_response_counts": dict(sorted(failed_counts.items())),
                    "ambiguous_response_counts": dict(sorted(ambiguous_counts.items())),
                    "suppressed_response_counts": dict(sorted(suppressed_counts.items())),
                    "response_ids": [item.id for item in linked],
                    "trigger_attribution": [
                        item.metadata_json.get("trigger_attribution") for item in linked
                    ],
                    "age_seconds": age_seconds,
                    "updated_at": decision.updated_at,
                }
            )

    linked_responses = [item for item in responses if item.trigger_source_event_id in source_ids]
    unlinked_responses = [item for item in responses if item.trigger_source_event_id is None]
    outside_window_responses = [
        item
        for item in responses
        if item.trigger_source_event_id is not None and item.trigger_source_event_id not in source_ids
    ]
    return {
        "since": since,
        "hours": hours,
        "grace_seconds": grace_seconds,
        "decisions": len(decision_rows),
        "outcomes": dict(sorted(outcome_counts.items())),
        "responses": {
            "total": len(responses),
            "linked": len(linked_responses),
            "unlinked": len(unlinked_responses),
            "linked_outside_decision_window": len(outside_window_responses),
            "trigger_attribution": dict(
                sorted(
                    Counter(
                        str(item.metadata_json.get("trigger_attribution") or "none")
                        for item in responses
                    ).items()
                )
            ),
            "unlinked_by_instance": dict(
                sorted(Counter(item.instance_id for item in unlinked_responses).items())
            ),
        },
        "details": details,
    }


@router.get("/v1/command-registry", dependencies=[Depends(require_admin)])
async def command_registry(request: Request) -> dict:
    try:
        registry = load_command_registry(request.app.state.settings.command_registry_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"command registry unavailable: {type(exc).__name__}",
        ) from exc
    return registry.as_dict()


@router.get("/v1/command-registry/runtime", dependencies=[Depends(require_admin)])
async def runtime_command_registry(request: Request, session: Session) -> dict:
    rows = (
        await session.scalars(
            select(CommandRegistrySnapshot).order_by(
                CommandRegistrySnapshot.instance_id,
                desc(CommandRegistrySnapshot.received_at),
            )
        )
    ).all()
    latest: dict[str, CommandRegistrySnapshot] = {}
    for row in rows:
        latest.setdefault(row.instance_id, row)

    try:
        registry = load_command_registry(request.app.state.settings.command_registry_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"command registry unavailable: {type(exc).__name__}",
        ) from exc

    snapshots = []
    combined_aliases: set[str] = set()
    uncovered_candidates: list[dict] = []
    now = datetime.now(timezone.utc)
    for row in latest.values():
        aliases = runtime_plugin_aliases(row.plugins_json)
        combined_aliases.update(aliases)
        for candidate in row.candidates_json:
            uncovered = [
                trigger
                for trigger in candidate.get("triggers", [])
                if not runtime_candidate_trigger_reviewed(registry, candidate, trigger)
            ]
            if uncovered:
                uncovered_candidates.append(
                    {**candidate, "instance_id": row.instance_id, "uncovered_triggers": uncovered}
                )
        received_at = row.received_at
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((now - received_at).total_seconds()))
        snapshots.append(
            {
                "instance_id": row.instance_id,
                "snapshot_hash": row.snapshot_hash,
                "observed_at": row.observed_at,
                "received_at": row.received_at,
                "age_seconds": age_seconds,
                "status": (
                    "fresh"
                    if age_seconds <= request.app.state.settings.command_registry_snapshot_stale_seconds
                    else "stale"
                ),
                "plugins": row.plugins_json,
                "candidates": row.candidates_json,
            }
        )

    static_rules = []
    serialized_rules = {item["id"]: item for item in registry.as_dict()["rules"]}
    for rule in registry.rules:
        payload = dict(serialized_rules[rule.id])
        payload["runtime_loaded"] = source_plugin_loaded(rule.source_plugin, combined_aliases) if latest else None
        static_rules.append(payload)

    return {
        "registry_version": registry.version,
        "snapshots": snapshots,
        "static_rules": static_rules,
        "uncovered_candidates": uncovered_candidates,
        "summary": {
            "snapshot_instances": len(snapshots),
            "fresh_snapshot_instances": sum(1 for item in snapshots if item["status"] == "fresh"),
            "stale_snapshot_instances": sum(1 for item in snapshots if item["status"] == "stale"),
            "loaded_plugins": sum(len(item["plugins"]) for item in snapshots),
            "runtime_matchers": sum(
                int(plugin.get("matcher_count", 0))
                for item in snapshots
                for plugin in item["plugins"]
            ),
            "unclassified_matchers": sum(
                max(
                    0,
                    int(plugin.get("matcher_count", 0))
                    - int(plugin.get("classified_matcher_count", 0)),
                )
                for item in snapshots
                for plugin in item["plugins"]
            ),
            "runtime_candidates": sum(len(item["candidates"]) for item in snapshots),
            "incomplete_runtime_candidates": sum(
                1
                for item in snapshots
                for candidate in item["candidates"]
                if candidate.get("complete") is not True
            ),
            "uncovered_candidate_triggers": sum(len(item["uncovered_triggers"]) for item in uncovered_candidates),
            "static_rules_loaded": sum(1 for item in static_rules if item["runtime_loaded"] is True),
            "static_rules_not_loaded": sum(1 for item in static_rules if item["runtime_loaded"] is False),
        },
    }


@router.get("/v1/events/{source_event_id}/context", dependencies=[Depends(require_admin)])
async def event_context(source_event_id: str, session: Session) -> dict:
    source = await session.get(SourceEvent, source_event_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source_event_id not found")

    observations = (
        await session.scalars(
            select(EventObservation)
            .where(EventObservation.source_event_id == source_event_id)
            .order_by(EventObservation.received_at)
        )
    ).all()
    links = (
        await session.scalars(
            select(EventLink).where(EventLink.from_source_event_id == source_event_id).order_by(EventLink.created_at)
        )
    ).all()
    decisions = (
        await session.scalars(
            select(EventDecision)
            .where(EventDecision.source_event_id == source_event_id)
            .order_by(EventDecision.created_at)
        )
    ).all()
    responses = (
        await session.scalars(
            select(ResponseRecord)
            .where(ResponseRecord.trigger_source_event_id == source_event_id)
            .order_by(ResponseRecord.received_at)
        )
    ).all()
    claims = (
        await session.scalars(
            select(EventClaim)
            .where(EventClaim.source_event_id == source_event_id)
            .order_by(EventClaim.created_at)
        )
    ).all()

    return {
        "source_event": {
            "source_event_id": source.id,
            "platform": source.platform,
            "event_type": source.event_type,
            "conversation_id": source.conversation_id,
            "conversation_type": source.conversation_type,
            "correlation_version": source.correlation_version,
            "occurred_at": source.occurred_at,
            "first_received_at": source.first_received_at,
        },
        "observations": [
            {
                "observation_id": item.id,
                "reported_source_event_id": item.reported_source_event_id,
                "instance_id": item.instance_id,
                "bot_id": item.bot_id,
                "platform_message_id": item.platform_message_id,
                "native_identity": item.metadata_json.get("native_identity"),
                "correlation_diagnostic": item.metadata_json.get("correlation"),
                "sender_id": item.sender_id,
                "sender_name": item.sender_name,
                "text": item.text,
                "received_at": item.received_at,
            }
            for item in observations
        ],
        "links": [
            {
                "link_id": item.id,
                "relation_type": item.relation_type,
                "to_source_event_id": item.to_source_event_id,
                "target_platform_message_id": item.target_platform_message_id,
                "resolver_status": item.resolver_status,
            }
            for item in links
        ],
        "decisions": [
            {
                "decision_id": item.id,
                "policy_version": item.policy_version,
                "decision_type": item.decision_type,
                "target_instance_id": item.target_instance_id,
                "confidence": item.confidence,
                "reason": item.reason,
                "features": item.features_json,
                "revision": item.revision,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
            for item in decisions
        ],
        "responses": [
            {
                "response_id": item.id,
                "source_response_id": item.source_response_id,
                "instance_id": item.instance_id,
                "response_type": item.response_type,
                "platform_message_id": item.platform_message_id,
                "text": item.text,
                "success": item.success,
                "received_at": item.received_at,
            }
            for item in responses
        ],
        "claims": [claim_record_payload(item) for item in claims],
    }


@router.get("/v1/instances", dependencies=[Depends(require_admin)])
async def instances(request: Request, session: Session) -> list[dict]:
    rows = (await session.scalars(select(BotInstance).order_by(BotInstance.id))).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "instance_id": item.id,
            "platform": item.platform,
            "adapter": item.adapter,
            "bot_id": item.bot_id,
            "role": item.role,
            "display_name": item.display_name,
            "version": item.version,
            "status": effective_status(item, request.app.state.settings.stale_after_seconds),
            "reported_status": item.reported_status,
            "heartbeat_age_seconds": (
                None
                if item.last_heartbeat_at is None
                else max(
                    0,
                    int(
                        (
                            now
                            - (
                                item.last_heartbeat_at
                                if item.last_heartbeat_at.tzinfo
                                else item.last_heartbeat_at.replace(tzinfo=timezone.utc)
                            )
                        ).total_seconds()
                    ),
                )
            ),
            "last_heartbeat_at": item.last_heartbeat_at,
            "last_event_at": item.last_event_at,
            "last_response_at": item.last_response_at,
            "capabilities": item.metadata_json.get("capabilities"),
            "ingress_spool": item.metadata_json.get("ingress_spool"),
        }
        for item in rows
    ]


@router.get("/v1/tools", dependencies=[Depends(require_admin)])
async def tools(request: Request, session: Session) -> dict:
    return await tool_registry_view(session, request.app.state.settings)


@router.get("/v1/tools/{tool_id}", dependencies=[Depends(require_admin)])
async def tool_detail(tool_id: str, request: Request, session: Session) -> dict:
    result = await tool_registry_view(session, request.app.state.settings, tool_id=tool_id)
    if not result["tools"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool_id not found")
    return {
        "schema_version": result["schema_version"],
        "execution": result["execution"],
        "tool_id": tool_id,
        "versions": result["tools"],
    }


@router.post("/v1/agent-runs", status_code=status.HTTP_201_CREATED)
async def post_agent_run(
    payload: AgentRunCreateIn,
    response: Response,
    session: Session,
    authenticated_caller: ToolInvocationIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    run, duplicate = await create_agent_run(
        session,
        payload,
        authenticated_caller,
        idempotency_key,
        session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await agent_run_view(session, run)
    result["duplicate"] = duplicate
    return result


@router.post("/v1/agent-interactions/evaluate", status_code=status.HTTP_202_ACCEPTED)
async def post_agent_interaction(
    payload: EventIn,
    response: Response,
    session: Session,
    authenticated_instance: Identity,
    idempotency_key: IdempotencyKey,
) -> dict:
    _verify_identity(authenticated_instance, payload.instance.instance_id)
    observation, event_duplicate = await ingest_event(
        session,
        payload,
        idempotency_key,
        session.info["settings"],
    )
    interaction, duplicate, reason = await accept_agent_interaction(
        session,
        payload,
        observation,
        authenticated_instance=authenticated_instance,
        settings=session.info["settings"],
    )
    receipt = await ingress_receipt_view(
        session,
        observation,
        duplicate=event_duplicate,
    )
    if interaction is None:
        response.status_code = status.HTTP_200_OK
        return {
            "schema_version": "1.0",
            "accepted": False,
            "duplicate": False,
            "reason_code": reason,
            "ingest_receipt": receipt,
        }
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = interaction_view(interaction, duplicate=duplicate)
    result["ingest_receipt"] = receipt
    return result


@router.post("/v1/agent-text-deliveries/lease", response_model=None)
async def post_agent_text_delivery_lease(
    payload: AgentTextDeliveryLeaseIn,
    session: Session,
    authenticated_instance: Identity,
) -> Response | dict:
    _verify_identity(authenticated_instance, payload.instance_id)
    lease = await lease_agent_delivery(
        session,
        instance_id=authenticated_instance,
        settings=session.info["settings"],
    )
    if lease is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return lease.model_dump(mode="json")


@router.post("/v1/agent-text-deliveries/{intent_id}/complete")
async def post_agent_text_delivery_complete(
    intent_id: str,
    payload: AgentTextDeliveryCompleteIn,
    session: Session,
    authenticated_instance: Identity,
) -> dict:
    intent = await complete_agent_delivery(
        session,
        intent_id,
        payload,
        authenticated_instance=authenticated_instance,
    )
    return {
        "schema_version": "1.0",
        "intent_id": intent.id,
        "state": intent.state,
        "platform_message_id": intent.platform_message_id,
        "safe_error_code": intent.safe_error_code,
        "terminal_at": intent.terminal_at,
    }


@router.get(
    "/v1/agent-interactions/{interaction_id}",
    dependencies=[Depends(require_admin)],
)
async def get_agent_interaction(interaction_id: str, session: Session) -> dict:
    interaction = await session.get(AgentInteraction, interaction_id)
    if interaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent interaction not found",
        )
    return interaction_view(interaction)


@router.get(
    "/v1/agent-runs/{run_id}",
    dependencies=[Depends(require_admin)],
)
async def get_agent_run(
    run_id: str,
    session: Session,
) -> dict:
    return await agent_run_view(
        session,
        await get_agent_run_for_admin(session, run_id),
    )


@router.get("/v1/agent-runs/{run_id}/planner-input")
async def get_agent_planner_input(
    run_id: str,
    session: Session,
    authenticated_provider: ModelProviderIdentity,
) -> dict:
    context, active_profile, active_profile_hash = await planner_input_for_provider(
        session,
        run_id,
        authenticated_provider,
        session.info["settings"],
    )
    run = await get_agent_run_for_admin(session, run_id)
    return {
        "schema_version": "1.0",
        "run_id": run.id,
        "context_hash": run.context_hash,
        "context": context.model_dump(mode="json"),
        "budget": run.budget_snapshot_json,
        "budget_hash": run.budget_hash,
        "model_profile": active_profile.model_dump(mode="json"),
        "model_profile_hash": active_profile_hash,
        "routing_reason": run.routing_reason,
        "deadline_at": run.deadline_at,
        "tool_execution_authority": False,
        "delivery_authority": False,
    }


@router.post(
    "/v1/agent-runs/{run_id}/attempts",
    status_code=status.HTTP_201_CREATED,
)
async def post_agent_attempt(
    run_id: str,
    payload: AgentAttemptReportIn,
    response: Response,
    session: Session,
    authenticated_provider: ModelProviderIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    attempt, run, duplicate = await record_agent_attempt(
        session,
        run_id,
        payload,
        provider_id=authenticated_provider,
        idempotency_key=idempotency_key,
        settings=session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await agent_run_view(session, run)
    result["attempt_id"] = attempt.id
    result["duplicate"] = duplicate
    return result


@router.post(
    "/v1/agent-runs/{run_id}/tool-loop",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def post_agent_tool_loop(
    run_id: str,
    payload: AgentToolPromotionIn,
    response: Response,
    session: Session,
) -> dict:
    loop, duplicate = await promote_wolfram_proposal(
        session,
        run_id,
        payload,
        session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await agent_tool_loop_view(session, loop)
    result["duplicate"] = duplicate
    return result


@router.get(
    "/v1/agent-tool-loops/{loop_id}",
    dependencies=[Depends(require_admin)],
)
async def get_agent_tool_loop(loop_id: str, session: Session) -> dict:
    loop = await session.get(AgentToolLoop, loop_id)
    if loop is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent tool loop not found",
        )
    return await agent_tool_loop_view(session, loop)


@router.get("/v1/agent-tool-loops/{loop_id}/planner-input")
async def get_agent_tool_loop_planner_input(
    loop_id: str,
    session: Session,
    authenticated_provider: ModelProviderIdentity,
) -> dict:
    return await continuation_input(
        session,
        loop_id,
        authenticated_provider,
        session.info["settings"],
    )


@router.post(
    "/v1/agent-tool-loops/{loop_id}/attempts",
    status_code=status.HTTP_201_CREATED,
)
async def post_agent_tool_loop_attempt(
    loop_id: str,
    payload: AgentAttemptReportIn,
    response: Response,
    session: Session,
    authenticated_provider: ModelProviderIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    continuation, loop, duplicate = await record_continuation(
        session,
        loop_id,
        payload,
        provider_id=authenticated_provider,
        idempotency_key=idempotency_key,
        settings=session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await agent_tool_loop_view(session, loop)
    result["continuation_id"] = continuation.id
    result["duplicate"] = duplicate
    return result


@router.post("/v1/tool-invocations", status_code=status.HTTP_201_CREATED)
async def post_tool_invocation(
    payload: ToolInvocationCreateIn,
    response: Response,
    session: Session,
    authenticated_caller: ToolInvocationIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    invocation, duplicate = await create_tool_invocation(
        session,
        payload,
        authenticated_caller,
        idempotency_key,
        session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await invocation_view(session, invocation)
    result["duplicate"] = duplicate
    return result


@router.get("/v1/tool-invocations/{invocation_id}")
async def get_tool_invocation_view(
    invocation_id: str,
    session: Session,
    authenticated_caller: ToolInvocationIdentity,
) -> dict:
    invocation = await get_tool_invocation(session, invocation_id, authenticated_caller)
    result = await invocation_view(session, invocation)
    result["attempts"] = await attempt_views(session, invocation.id)
    return result


@router.post("/v1/tool-invocations/{invocation_id}/cancel")
async def post_tool_invocation_cancel(
    invocation_id: str,
    payload: ToolInvocationCancelIn,
    session: Session,
    authenticated_caller: ToolInvocationIdentity,
) -> dict:
    invocation = await cancel_tool_invocation(
        session,
        invocation_id,
        authenticated_caller,
        payload.reason,
    )
    result = await invocation_view(session, invocation)
    result["attempts"] = await attempt_views(session, invocation.id)
    return result


@router.post("/v1/tool-invocations/{invocation_id}/confirm")
async def post_tool_invocation_confirm(
    invocation_id: str,
    payload: ToolInvocationConfirmIn,
    response: Response,
    session: Session,
    authenticated_caller: ToolInvocationIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    invocation, duplicate = await decide_tool_confirmation(
        session,
        invocation_id,
        payload,
        authenticated_caller,
        idempotency_key,
        session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = await invocation_view(session, invocation)
    result["attempts"] = await attempt_views(session, invocation.id)
    result["duplicate"] = duplicate
    return result


@router.post("/v1/tool-executions/lease", response_model=None)
async def post_tool_execution_lease(
    payload: ToolLeaseRequestIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> Response | dict:
    lease = await lease_tool_execution(
        session,
        payload,
        authenticated_provider,
        session.info["settings"],
    )
    if lease is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return lease.model_dump(mode="json")


@router.post("/v1/tool-executions/{invocation_id}/start")
async def post_tool_execution_start(
    invocation_id: str,
    payload: ToolExecutionStartIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    return await start_tool_execution(
        session,
        invocation_id,
        payload,
        authenticated_provider,
    )


@router.post("/v1/tool-executions/{invocation_id}/heartbeat")
async def post_tool_execution_heartbeat(
    invocation_id: str,
    payload: ToolExecutionHeartbeatIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    return await heartbeat_tool_execution(
        session,
        invocation_id,
        payload,
        authenticated_provider,
        session.info["settings"],
    )


@router.post(
    "/v1/tool-executions/{invocation_id}/artifacts/reserve",
    status_code=status.HTTP_201_CREATED,
)
async def post_tool_artifact_reserve(
    invocation_id: str,
    payload: ToolArtifactReserveIn,
    response: Response,
    session: Session,
    authenticated_provider: ProviderIdentity,
    idempotency_key: IdempotencyKey,
) -> dict:
    reservation, duplicate = await reserve_tool_artifact(
        session,
        invocation_id,
        payload,
        authenticated_provider,
        idempotency_key,
        session.info["settings"],
    )
    if duplicate:
        response.status_code = status.HTTP_200_OK
    result = reservation.model_dump(mode="json")
    result["duplicate"] = duplicate
    return result


@router.put("/v1/tool-artifacts/{artifact_id}/content")
async def put_tool_artifact_content(
    artifact_id: str,
    request: Request,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    upload_secret = request.headers.get("X-Superlily-Artifact-Upload-Secret", "")
    if not _ARTIFACT_UPLOAD_SECRET_RE.fullmatch(upload_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="artifact upload authorization is invalid",
        )
    content_type = request.headers.get("Content-Type", "")
    raw_content_length = request.headers.get("Content-Length")
    if raw_content_length is None:
        content_length = None
    elif (
        len(raw_content_length) > 20
        or not raw_content_length.isascii()
        or not raw_content_length.isdigit()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="artifact Content-Length is invalid",
        )
    else:
        content_length = int(raw_content_length)
    result = await upload_tool_artifact(
        session,
        artifact_id,
        request.stream(),
        authenticated_provider,
        upload_secret,
        content_type,
        content_length,
        session.info["settings"],
    )
    return result.model_dump(mode="json")


@router.post("/v1/tool-executions/{invocation_id}/artifacts/finalize")
async def post_tool_artifact_finalize(
    invocation_id: str,
    payload: ToolArtifactFinalizeIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    result = await finalize_tool_artifact(
        session,
        invocation_id,
        payload,
        authenticated_provider,
        session.info["settings"],
    )
    return result.model_dump(mode="json")


@router.post("/v1/tool-executions/{invocation_id}/complete")
async def post_tool_execution_complete(
    invocation_id: str,
    payload: ToolExecutionCompleteIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    return await complete_tool_execution(
        session,
        invocation_id,
        payload,
        authenticated_provider,
        session.info["settings"],
    )


@router.post("/v1/tool-executions/{invocation_id}/fail")
async def post_tool_execution_fail(
    invocation_id: str,
    payload: ToolExecutionFailIn,
    session: Session,
    authenticated_provider: ProviderIdentity,
) -> dict:
    return await fail_tool_execution(
        session,
        invocation_id,
        payload,
        authenticated_provider,
    )
