"""Phase 4 command client with exact reviewed tool identities.

This module has no NoneBot dependency so request, polling, artifact integrity,
and at-most-once delivery-intent behavior can be tested deterministically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

import httpx


TERMINAL_INVOCATION_STATES = frozenset(
    {
        "rejected",
        "recorded_only",
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "unknown_completion",
        "expired",
    }
)


@dataclass(frozen=True, slots=True)
class ReviewedTool:
    tool_id: str
    descriptor_version: str
    descriptor_hash: str
    timeout_seconds: float


REVIEWED_TOOLS = {
    "status.inspect": ReviewedTool(
        tool_id="status.inspect",
        descriptor_version="1.0.2",
        descriptor_hash="0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd",
        timeout_seconds=10.0,
    ),
    "wolfram.run": ReviewedTool(
        tool_id="wolfram.run",
        descriptor_version="1.0.0",
        descriptor_hash="aa6e9b1c930406bab11500de6c7653219aa9e8b831ee5fc7d08b1ab3d239ddaa",
        timeout_seconds=70.0,
    ),
    "latex.render": ReviewedTool(
        tool_id="latex.render",
        descriptor_version="1.0.0",
        descriptor_hash="adad493e24444f8a09215180dc90839102646fe069e2d767f9d1cab9ef826b36",
        timeout_seconds=45.0,
    ),
}


class Phase4CommandFallback(RuntimeError):
    """The bridge has not sent anything and may safely use the legacy matcher."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Phase4CommandSuppressed(RuntimeError):
    """Core already owns this delivery identity; legacy must not send again."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    intent_id: str
    selected_family: str
    content: bytes | str
    artifact_id: str
    delivery_plan_id: str


class Phase4CommandClient:
    def __init__(
        self,
        core_url: str,
        token: str,
        *,
        request_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.25,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.core_url = core_url.rstrip("/")
        self.token = token
        self.request_timeout_seconds = request_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.transport = transport

    @property
    def _authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> httpx.Response:
        headers = dict(self._authorization)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        timeout = httpx.Timeout(
            self.request_timeout_seconds,
            connect=min(5.0, self.request_timeout_seconds),
        )
        async with httpx.AsyncClient(
            base_url=self.core_url,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            response = await client.request(
                method,
                path,
                json=json_payload,
                headers=headers,
            )
        return response

    async def _receipt_to_delivery(
        self,
        receipt: dict[str, Any],
        *,
        instance_id: str,
        idempotency_key: str,
    ) -> PreparedDelivery:
        artifact_id = receipt.get("artifact_id")
        content_path = receipt.get("content_path")
        expected_hash = receipt.get("content_sha256")
        delivery_plan_id = receipt.get("delivery_plan_id")
        plan = receipt.get("delivery_plan")
        if (
            not isinstance(artifact_id, str)
            or not isinstance(content_path, str)
            or not content_path.startswith("/v1/render-artifacts/")
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or not isinstance(delivery_plan_id, str)
            or not isinstance(plan, dict)
        ):
            raise Phase4CommandFallback("invalid_render_receipt")

        intent_response = await self._request(
            "POST",
            f"/v1/render-artifacts/{artifact_id}/delivery-intents",
            json_payload={
                "instance_id": instance_id,
                "delivery_plan_id": delivery_plan_id,
                "idempotency_key": f"{idempotency_key}:delivery",
            },
        )
        if intent_response.status_code not in {200, 201}:
            raise Phase4CommandFallback("delivery_intent_rejected")
        intent = intent_response.json()
        if intent.get("should_send") is not True:
            raise Phase4CommandSuppressed(str(intent.get("status", "unknown")))
        intent_id = intent.get("intent_id")
        family = plan.get("selected_family")
        if not isinstance(intent_id, str):
            raise Phase4CommandFallback("invalid_delivery_intent")
        if family == "image":
            artifact_response = await self._request("GET", content_path)
            if artifact_response.status_code != 200:
                raise Phase4CommandFallback("artifact_download_failed")
            content = artifact_response.content
            if (
                not content.startswith(b"\x89PNG\r\n\x1a\n")
                or sha256(content).hexdigest() != expected_hash
            ):
                raise Phase4CommandFallback("artifact_integrity_failure")
        elif family == "text" and isinstance(plan.get("fallback_text"), str):
            content = plan["fallback_text"]
        else:
            raise Phase4CommandFallback("unsupported_delivery_plan")
        return PreparedDelivery(
            intent_id=intent_id,
            selected_family=family,
            content=content,
            artifact_id=artifact_id,
            delivery_plan_id=delivery_plan_id,
        )

    async def prepare_tool_delivery(
        self,
        *,
        instance_id: str,
        conversation_key: str,
        source_event_id: str,
        sender_id: str,
        platform_roles: list[str],
        tool_id: str,
        tool_input: dict[str, Any],
        idempotency_key: str,
    ) -> PreparedDelivery:
        tool = REVIEWED_TOOLS[tool_id]
        adapter, adapter_separator, remainder = conversation_key.partition("-")
        conversation_type, type_separator, native_id = remainder.partition("_")
        if (
            not adapter_separator
            or not adapter
            or not type_separator
            or conversation_type != "group"
            or not native_id
        ):
            raise Phase4CommandFallback("unsupported_conversation")
        invocation = {
            "schema_version": "1.0",
            "tool_id": tool.tool_id,
            "descriptor_version": tool.descriptor_version,
            "descriptor_hash": tool.descriptor_hash,
            "input": tool_input,
            "principal": {
                "platform": "qq",
                "sender_id": sender_id,
                "conversation_id": f"group:{native_id}",
                "conversation_type": "group",
                "platform_roles": platform_roles,
                "source_event_id": source_event_id,
            },
            "capabilities": [],
        }
        response = await self._request(
            "POST",
            "/v1/tool-invocations",
            json_payload=invocation,
            idempotency_key=f"{idempotency_key}:invoke",
        )
        if response.status_code not in {200, 201}:
            raise Phase4CommandFallback(f"invocation_http_{response.status_code}")
        view = response.json()
        invocation_id = view.get("invocation_id")
        state = view.get("state")
        if not isinstance(invocation_id, str) or not isinstance(state, str):
            raise Phase4CommandFallback("invalid_invocation_receipt")

        deadline = asyncio.get_running_loop().time() + tool.timeout_seconds
        while state not in TERMINAL_INVOCATION_STATES:
            if asyncio.get_running_loop().time() >= deadline:
                raise Phase4CommandFallback("invocation_poll_timeout")
            await asyncio.sleep(self.poll_interval_seconds)
            response = await self._request(
                "GET",
                f"/v1/tool-invocations/{invocation_id}",
            )
            if response.status_code != 200:
                raise Phase4CommandFallback(
                    f"invocation_poll_http_{response.status_code}"
                )
            view = response.json()
            state = view.get("state")
            if not isinstance(state, str):
                raise Phase4CommandFallback("invalid_invocation_state")
        if state != "succeeded":
            raise Phase4CommandFallback(f"invocation_{state}")

        response = await self._request(
            "POST",
            f"/v1/tool-invocations/{invocation_id}/render-result",
            json_payload={
                "schema_version": "1.0",
                "instance_id": instance_id,
                "conversation_key": conversation_key,
                "source_event_id": source_event_id,
            },
            idempotency_key=f"{idempotency_key}:render",
        )
        if response.status_code not in {200, 201}:
            raise Phase4CommandFallback(f"render_http_{response.status_code}")
        return await self._receipt_to_delivery(
            response.json(),
            instance_id=instance_id,
            idempotency_key=idempotency_key,
        )

    async def prepare_help_delivery(
        self,
        *,
        instance_id: str,
        conversation_key: str,
        source_event_id: str,
        commands: list[dict[str, str]],
        idempotency_key: str,
    ) -> PreparedDelivery:
        response = await self._request(
            "POST",
            "/v1/help-documents",
            json_payload={
                "schema_version": "1.0",
                "instance_id": instance_id,
                "conversation_key": conversation_key,
                "source_event_id": source_event_id,
                "title": "莉莉命令帮助",
                "commands": commands,
            },
            idempotency_key=f"{idempotency_key}:render",
        )
        if response.status_code not in {200, 201}:
            raise Phase4CommandFallback(f"help_render_http_{response.status_code}")
        return await self._receipt_to_delivery(
            response.json(),
            instance_id=instance_id,
            idempotency_key=idempotency_key,
        )

    async def complete_delivery(
        self,
        *,
        instance_id: str,
        intent_id: str,
        outcome: str,
        platform_message_id: str | None = None,
        safe_error_code: str | None = None,
    ) -> bool:
        response = await self._request(
            "POST",
            f"/v1/render-delivery-intents/{intent_id}/complete",
            json_payload={
                "instance_id": instance_id,
                "outcome": outcome,
                "platform_message_id": platform_message_id,
                "safe_error_code": safe_error_code,
            },
        )
        return response.status_code == 200


def command_idempotency_key(
    *,
    instance_id: str,
    source_event_id: str,
    command: str,
    arguments: dict[str, Any],
) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(
        "\x1f".join(
            (instance_id, source_event_id, command, canonical)
        ).encode("utf-8")
    ).hexdigest()
    return f"phase4-command:{digest}"
