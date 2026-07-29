"""Drive one no-send Phase 5a shadow or Phase 5b Wolfram acceptance probe.

The driver deliberately cannot activate registry resources, mutate service
configuration, or address a platform conversation. It only accepts a loopback
Core URL, creates a fixed ``system`` conversation, and emits a bounded evidence
summary without prompts, model output, tool output, or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from superlily_model_provider import DeepSeekPlanner, DeepSeekPlannerConfig


PROFILE_ID = "deepseek-v4-pro"
PROFILE_VERSION = "1.0.0"
PROFILE_HASH = "948f9b7cd20394f0607d1bb347f776e80f5b5e307c381223b8a40d3bf735bec3"
TOOL_ID = "wolfram.run"
DESCRIPTOR_VERSION = "1.1.0"
DESCRIPTOR_HASH = "ec3375907804f588d765ed643b9c8481eb2d4a578924a614652cca64d0414da4"
PROVIDER_ID = "provider-wolfram-primary"
CONVERSATION_ID = "phase5-acceptance"
CANONICAL_CONVERSATION = f"system:system:{CONVERSATION_ID}"
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AcceptanceError(RuntimeError):
    """A bounded failure that never includes response bodies or credentials."""


@dataclass(frozen=True, slots=True)
class AcceptanceConfig:
    scenario: str
    core_url: str
    admin_token: str = field(repr=False)
    ingest_token: str = field(repr=False)
    model_token: str = field(repr=False)
    deepseek_api_key: str = field(repr=False)
    ingest_instance_id: str
    run_id: str
    source_commit: str
    wait_seconds: float
    expected_plan_id: str | None = None
    expected_plan_hash: str | None = None


def _budget(*, bounded: bool) -> dict[str, Any]:
    return {
        "max_model_attempts": 2 if bounded else 1,
        "max_model_turns": 2 if bounded else 1,
        "max_tool_proposals": 1 if bounded else 3,
        "max_tool_calls": 1 if bounded else 0,
        "max_sequential_depth": 1 if bounded else 0,
        "max_parallel_fanout": 1 if bounded else 0,
        "max_wall_time_ms": 180_000,
        "max_input_tokens": 8_192,
        "max_output_tokens": 2_048,
        "max_total_tokens": 10_240,
        "max_cost_microunits": 100_000,
        "max_input_bytes": 131_072,
        "max_output_bytes": 65_536,
        "max_result_bytes": 16_384 if bounded else 0,
        "max_artifact_bytes": 0,
    }


def _event(config: AcceptanceConfig, *, bounded: bool) -> dict[str, Any]:
    question = (
        "请必须使用当前有资格的 wolfram.run 精确计算 2+2；"
        "拿到结果后只形成最终答案，不发送平台消息。"
        if bounded
        else (
            "请直接回答：2+2等于多少？"
            "不要执行工具，也不要发送平台消息。"
        )
    )
    return {
        "schema_version": "1.0",
        "source_event_id": f"phase5:acceptance:{config.scenario}:{config.run_id}",
        "instance": {
            "instance_id": config.ingest_instance_id,
            "platform": "system",
            "adapter": "phase5_acceptance",
            "bot_id": "superlily",
            "role": "talk",
        },
        "event_type": "message",
        "conversation": {
            "id": CONVERSATION_ID,
            "type": "system",
            "name": "Phase 5 Acceptance",
        },
        "sender": {
            "id": "phase5-acceptance",
            "name": "Phase 5 Acceptance",
            "roles": [],
        },
        "message": {
            "id": config.run_id,
            "text": question,
            "segments": [{"type": "text", "data": {"text": question}}],
            "attachments": [],
        },
        "references": [],
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "probe": config.scenario,
            "no_platform_delivery": True,
            "source_commit": config.source_commit,
        },
    }


def _attempt_summary(attempt: dict[str, Any]) -> dict[str, Any]:
    usage = attempt.get("usage")
    return {
        "attempt_number": attempt.get("attempt_number"),
        "provider_id": attempt.get("provider_id"),
        "outcome": attempt.get("outcome"),
        "model_request_id": attempt.get("model_request_id"),
        "safe_error_code": attempt.get("safe_error_code"),
        "usage": usage if isinstance(usage, dict) else None,
    }


class Phase5AcceptanceDriver:
    def __init__(
        self,
        config: AcceptanceConfig,
        *,
        core_transport: httpx.AsyncBaseTransport | None = None,
        model_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            base_url=config.core_url.rstrip("/"),
            timeout=10.0,
            trust_env=False,
            transport=core_transport,
        )
        self._planner = DeepSeekPlanner(
            DeepSeekPlannerConfig(
                api_key=config.deepseek_api_key,
                timeout_seconds=min(config.wait_seconds, 180.0),
            ),
            transport=model_transport,
        )

    async def __aenter__(self) -> "Phase5AcceptanceDriver":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.__aexit__(None, None, None)

    async def _json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        expected_status: int,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = await self._client.request(
            method,
            path,
            headers=headers,
            json=payload,
        )
        if response.status_code != expected_status:
            raise AcceptanceError(
                f"Core {method} {path} returned HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise AcceptanceError("Core returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise AcceptanceError("Core returned a non-object response")
        return result

    async def _ingest_event(self, *, bounded: bool) -> str:
        receipt = await self._json(
            "POST",
            "/v1/events",
            token=self.config.ingest_token,
            expected_status=201,
            payload=_event(self.config, bounded=bounded),
            idempotency_key=f"phase5-event-{self.config.scenario}-{self.config.run_id}",
        )
        source_event_id = receipt.get("source_event_id")
        if not isinstance(source_event_id, str) or not source_event_id:
            raise AcceptanceError("event receipt omitted its source event ID")
        return source_event_id

    async def _create_run(self, source_event_id: str, *, bounded: bool) -> dict[str, Any]:
        run = await self._json(
            "POST",
            "/v1/agent-runs",
            token=self.config.admin_token,
            expected_status=201,
            idempotency_key=f"phase5-run-{self.config.scenario}-{self.config.run_id}",
            payload={
                "schema_version": "1.0",
                "source_event_id": source_event_id,
                "model_provider_id": PROFILE_ID,
                "model_profile_version": PROFILE_VERSION,
                "model_profile_hash": PROFILE_HASH,
                "fallback_model_profiles": [],
                "routing_reason": f"phase5_{self.config.scenario}_no_send",
                "budget": _budget(bounded=bounded),
            },
        )
        if run.get("conversation_key") != CANONICAL_CONVERSATION:
            raise AcceptanceError("Core did not preserve the fixed system conversation")
        if run.get("delivery_intent_count") != 0 or run.get("tool_invocation_count") != 0:
            raise AcceptanceError("new AgentRun already contains execution or delivery")
        if run.get("model_profile_hash") != PROFILE_HASH:
            raise AcceptanceError("AgentRun did not freeze the reviewed model profile")
        return run

    async def _model_attempt(
        self,
        *,
        planner_path: str,
        report_path: str,
        idempotency_scope: str,
    ) -> dict[str, Any]:
        planner_input = await self._json(
            "GET",
            planner_path,
            token=self.config.model_token,
            expected_status=200,
        )
        if (
            planner_input.get("tool_execution_authority") is not False
            or planner_input.get("delivery_authority") is not False
        ):
            raise AcceptanceError("planner input unexpectedly grants authority")
        attempt = await self._planner.plan(planner_input)
        report = attempt.report
        report_key = hashlib.sha256(
            (
                f"{PROFILE_ID}:{idempotency_scope}:"
                f"{report.raw_output_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        result = await self._json(
            "POST",
            report_path,
            token=self.config.model_token,
            expected_status=201,
            payload=report.model_dump(mode="json"),
            idempotency_key=report_key,
        )
        if report.outcome != "succeeded":
            raise AcceptanceError(
                f"model attempt ended as {report.outcome}: {report.safe_error_code}"
            )
        return result

    async def _view_run(self, run_id: str) -> dict[str, Any]:
        return await self._json(
            "GET",
            f"/v1/agent-runs/{run_id}",
            token=self.config.admin_token,
            expected_status=200,
        )

    async def _verify_wolfram_authority(self) -> None:
        detail = await self._json(
            "GET",
            f"/v1/tools/{TOOL_ID}",
            token=self.config.admin_token,
            expected_status=200,
        )
        execution = detail.get("execution")
        active = execution.get("active_rollout_plan") if isinstance(execution, dict) else None
        if (
            not isinstance(execution, dict)
            or execution.get("mode") != "canary"
            or execution.get("leases_enabled") is not True
            or execution.get("natural_language_callers") is not True
            or not isinstance(active, dict)
            or active.get("plan_id") != self.config.expected_plan_id
            or active.get("plan_hash") != self.config.expected_plan_hash
            or active.get("max_invocations") != 1
            or active.get("consumed_invocations") != 0
        ):
            raise AcceptanceError("exact single-use Agent Wolfram plan is not active")
        versions = detail.get("versions")
        version = next(
            (
                item
                for item in versions
                if isinstance(item, dict) and item.get("version") == DESCRIPTOR_VERSION
            ),
            None,
        ) if isinstance(versions, list) else None
        if (
            not isinstance(version, dict)
            or version.get("desired", {}).get("descriptor_hash") != DESCRIPTOR_HASH
            or version.get("desired", {}).get("lifecycle") != "active"
            or version.get("desired", {}).get("allowed_callers")
            != ["command", "agent", "admin_api"]
            or version.get("desired", {}).get("natural_language") is not True
            or version.get("effective", {}).get("eligible") is not True
        ):
            raise AcceptanceError("wolfram.run@1.1.0 is not exact, active, and eligible")

    async def _wait_loop(self, loop_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.wait_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = await self._json(
                "GET",
                f"/v1/agent-tool-loops/{loop_id}",
                token=self.config.admin_token,
                expected_status=200,
            )
            if last.get("state") == "result_ready":
                return last
            if last.get("terminal_at") is not None:
                raise AcceptanceError(
                    f"Agent tool loop terminated as {last.get('state')}"
                )
            await asyncio.sleep(0.25)
        state = None if last is None else last.get("state")
        raise AcceptanceError(f"Agent tool loop timed out in state {state}")

    async def run_shadow(self) -> dict[str, Any]:
        source_event_id = await self._ingest_event(bounded=False)
        created = await self._create_run(source_event_id, bounded=False)
        run_id = str(created["run_id"])
        await self._model_attempt(
            planner_path=f"/v1/agent-runs/{run_id}/planner-input",
            report_path=f"/v1/agent-runs/{run_id}/attempts",
            idempotency_scope=run_id,
        )
        view = await self._view_run(run_id)
        if (
            view.get("state") != "shadow_complete"
            or view.get("attempt_count") != 1
            or view.get("tool_invocation_count") != 0
            or view.get("delivery_intent_count") != 0
        ):
            raise AcceptanceError("5a shadow did not converge with zero authority")
        return {
            "schema_version": "1.0",
            "scenario": "shadow",
            "source_commit": self.config.source_commit,
            "run_id": run_id,
            "conversation_key": view["conversation_key"],
            "state": view["state"],
            "reason_code": view["reason_code"],
            "profile_hash": view["model_profile_hash"],
            "attempts": [_attempt_summary(item) for item in view["attempts"]],
            "proposal_validation_counts": view["proposal_validation_counts"],
            "tool_invocation_count": view["tool_invocation_count"],
            "delivery_intent_count": view["delivery_intent_count"],
        }

    async def run_bounded_wolfram(self) -> dict[str, Any]:
        await self._verify_wolfram_authority()
        source_event_id = await self._ingest_event(bounded=True)
        created = await self._create_run(source_event_id, bounded=True)
        run_id = str(created["run_id"])
        planned = await self._model_attempt(
            planner_path=f"/v1/agent-runs/{run_id}/planner-input",
            report_path=f"/v1/agent-runs/{run_id}/attempts",
            idempotency_scope=run_id,
        )
        proposals = [
            item
            for item in planned.get("proposals", [])
            if isinstance(item, dict)
            and item.get("tool_id") == TOOL_ID
            and item.get("descriptor_version") == DESCRIPTOR_VERSION
            and item.get("descriptor_hash") == DESCRIPTOR_HASH
            and item.get("validation") == "valid"
        ]
        if len(proposals) != 1:
            raise AcceptanceError("model did not produce one exact valid Wolfram proposal")
        promoted = await self._json(
            "POST",
            f"/v1/agent-runs/{run_id}/tool-loop",
            token=self.config.admin_token,
            expected_status=201,
            payload={
                "schema_version": "1.0",
                "proposal_id": proposals[0]["proposal_id"],
            },
        )
        if (
            promoted.get("state") != "tool_pending"
            or promoted.get("tool_invocation_count") != 1
            or promoted.get("delivery_intent_count") != 0
        ):
            raise AcceptanceError("Wolfram proposal did not enter the bounded tool loop")
        loop_id = str(promoted["loop_id"])
        result_ready = await self._wait_loop(loop_id)
        completed = await self._model_attempt(
            planner_path=f"/v1/agent-tool-loops/{loop_id}/planner-input",
            report_path=f"/v1/agent-tool-loops/{loop_id}/attempts",
            idempotency_scope=loop_id,
        )
        if (
            completed.get("state") != "complete"
            or completed.get("tool_invocation_count") != 1
            or completed.get("delivery_intent_count") != 0
        ):
            raise AcceptanceError("5b continuation did not complete with zero delivery")
        invocation = await self._json(
            "GET",
            f"/v1/tool-invocations/{promoted['invocation_id']}",
            token=self.config.admin_token,
            expected_status=200,
        )
        if (
            invocation.get("state") != "succeeded"
            or invocation.get("selected_provider_id") != PROVIDER_ID
            or invocation.get("creator", {}).get("type") != "agent"
        ):
            raise AcceptanceError("the exact Agent Wolfram invocation did not succeed")
        run_view = await self._view_run(run_id)
        return {
            "schema_version": "1.0",
            "scenario": "bounded-wolfram",
            "source_commit": self.config.source_commit,
            "plan_id": self.config.expected_plan_id,
            "plan_hash": self.config.expected_plan_hash,
            "run_id": run_id,
            "loop_id": loop_id,
            "invocation_id": promoted["invocation_id"],
            "conversation_key": run_view["conversation_key"],
            "run_state": run_view["state"],
            "loop_state": completed["state"],
            "loop_reason_code": completed["reason_code"],
            "result_hash": result_ready["result_hash"],
            "result_bytes": result_ready["result_bytes"],
            "model_attempts": [
                _attempt_summary(item) for item in run_view["attempts"]
            ],
            "continuation_attempt_count": completed["continuation_attempt_count"],
            "tool_invocation_count": completed["tool_invocation_count"],
            "delivery_intent_count": completed["delivery_intent_count"],
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行一次后台、无平台发送的 Phase 5a/5b 验收探针",
    )
    parser.add_argument("scenario", choices=("shadow", "bounded-wolfram"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--ingest-instance-id", default="nekro-agent")
    parser.add_argument("--wait-seconds", type=float, default=120.0)
    parser.add_argument("--expected-plan-id")
    parser.add_argument("--expected-plan-hash")
    parser.add_argument(
        "--no-platform-send-ack",
        action="store_true",
        help="acknowledge that this probe must remain on its fixed system conversation",
    )
    return parser


def _config(args: argparse.Namespace) -> AcceptanceConfig:
    if not args.no_platform_send_ack:
        raise AcceptanceError("the no-platform-send boundary must be acknowledged")
    if not _RUN_ID_RE.fullmatch(args.run_id):
        raise AcceptanceError("run-id must be 6-64 lowercase letters, digits, or hyphens")
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_commit):
        raise AcceptanceError("source-commit must be one full lowercase Git commit")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", args.ingest_instance_id):
        raise AcceptanceError("ingest instance ID is invalid")
    if not 10 <= args.wait_seconds <= 180:
        raise AcceptanceError("wait-seconds must be between 10 and 180")
    if args.scenario == "bounded-wolfram":
        if not args.expected_plan_id or not re.fullmatch(
            r"[a-z][a-z0-9.-]{2,127}",
            args.expected_plan_id,
        ):
            raise AcceptanceError("bounded Wolfram requires an exact rollout plan ID")
        if not args.expected_plan_hash or not _SHA256_RE.fullmatch(
            args.expected_plan_hash
        ):
            raise AcceptanceError("bounded Wolfram requires an exact rollout plan hash")
    elif args.expected_plan_id is not None or args.expected_plan_hash is not None:
        raise AcceptanceError("shadow must not accept rollout plan authority")
    core_url = os.environ.pop("SUPERLILY_PHASE5_CORE_URL", "http://127.0.0.1:8765")
    parsed = urlsplit(core_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AcceptanceError("driver only accepts an explicit loopback HTTP Core URL")
    credentials = {
        "admin_token": os.environ.pop("SUPERLILY_ADMIN_TOKEN", ""),
        "ingest_token": os.environ.pop("SUPERLILY_PHASE5_INGEST_TOKEN", ""),
        "model_token": os.environ.pop("SUPERLILY_MODEL_PROVIDER_TOKEN", ""),
        "deepseek_api_key": os.environ.pop("SUPERLILY_DEEPSEEK_API_KEY", ""),
    }
    if any(
        not value or value != value.strip()
        for value in credentials.values()
    ):
        raise AcceptanceError("all four Phase 5 credentials are required in the environment")
    if len(set(credentials.values())) != len(credentials):
        raise AcceptanceError("Phase 5 credentials must be independent")
    return AcceptanceConfig(
        scenario=args.scenario,
        core_url=core_url,
        ingest_instance_id=args.ingest_instance_id,
        run_id=args.run_id,
        source_commit=args.source_commit,
        wait_seconds=args.wait_seconds,
        expected_plan_id=args.expected_plan_id,
        expected_plan_hash=args.expected_plan_hash,
        **credentials,
    )


async def _main(config: AcceptanceConfig) -> dict[str, Any]:
    async with Phase5AcceptanceDriver(config) as driver:
        if config.scenario == "shadow":
            return await driver.run_shadow()
        return await driver.run_bounded_wolfram()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_main(_config(args)))
    except AcceptanceError as exc:
        error = str(exc)
    except httpx.HTTPError:
        error = "Core or model HTTP transport failed"
    except (OSError, ValueError, KeyError, TypeError):
        error = "local bounded validation failed"
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        return 0
    print(
        json.dumps(
            {"schema_version": "1.0", "outcome": "failed", "error": error},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
