"""对 ``status.inspect@1.0.2`` 执行一次有界的第三阶段故障演练。

本程序只驱动已经由独立控制面激活的 Git-bound 单次计划。它不激活或暂停
计划，不管理容器，也不发送平台消息。Admin/Provider credential 只从环境
读取并立即从进程环境移除；输出证据不含 bearer token、lease secret、
请求/结果正文。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from superlily_contracts import (
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolLeaseOut,
    ToolUsage,
    canonicalize_json_value,
)
from superlily_provider_sdk import (
    ProviderExecutionClient,
    ProviderExecutionError,
    ProviderRegistryClient,
)
from superlily_status_provider.executor import StatusProcessSupervisor, SupervisedStatusResult
from superlily_status_provider.main import DEFAULT_DESCRIPTOR_PATH, _load_runtime
from superlily_status_provider.status import PROVIDER_ID


_SOURCE_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
TOOL_ID = "status.inspect"
DESCRIPTOR_VERSION = "1.0.2"
DESCRIPTOR_HASH = "0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd"
CONVERSATION = "qq:group:1080353942"
SCENARIOS = (
    "retry-fence-success",
    "invalid-output",
    "clock-skew-success",
    "cancel-acknowledged",
    "cancel-completion-race",
    "cancel-unacknowledged",
)
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,63}$")


class DrillError(RuntimeError):
    """不回显 credential 或 Core 原始响应的演练失败。"""


@dataclass(frozen=True, slots=True)
class DrillConfig:
    core_url: str
    admin_token: str = field(repr=False)
    provider_token: str = field(repr=False)
    descriptor_path: Path
    scenario: str
    expected_plan_id: str
    expected_plan_hash: str
    run_id: str
    wait_seconds: float


def _proof(lease: ToolLeaseOut) -> dict[str, Any]:
    return {
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.fencing_token,
        "lease_secret": lease.lease_secret,
    }


def _remaining_seconds(lease: ToolLeaseOut) -> float:
    return (lease.deadline_at - datetime.now(timezone.utc)).total_seconds()


def _safe_summary(
    view: dict[str, Any],
    *,
    scenario: str,
    plan_id: str,
    plan_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scenario": scenario,
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "invocation_id": view["invocation_id"],
        "state": view["state"],
        "reason_code": view["reason_code"],
        "deadline_at": view["deadline_at"],
        "terminal_at": view["terminal_at"],
        "transitions": [
            {
                "sequence": item["sequence"],
                "event": item["event"],
                "state": item["state"],
                "reason_code": item["reason_code"],
            }
            for item in view["transitions"]
        ],
        "attempts": [
            {
                "attempt_id": item["attempt_id"],
                "attempt_number": item["attempt_number"],
                "fencing_token": item["fencing_token"],
                "state": item["state"],
                "error_code": item["error_code"],
                "output_hash": item["output_hash"],
            }
            for item in view.get("attempts", [])
        ],
    }


class StatusFaultDriver:
    def __init__(self, config: DrillConfig) -> None:
        self._inspector, self._implementation = _load_runtime(
            config.descriptor_path,
            execution_enabled=True,
        )
        entry = self._implementation.inventory_entry
        if (
            entry.tool_id != TOOL_ID
            or entry.descriptor_version != DESCRIPTOR_VERSION
            or entry.descriptor_hash != DESCRIPTOR_HASH
        ):
            raise DrillError("local descriptor is not exact status.inspect@1.0.2 authority")
        self.config = config
        self._admin = httpx.AsyncClient(
            base_url=config.core_url.rstrip("/"),
            headers={"Authorization": f"Bearer {config.admin_token}"},
            timeout=5.0,
            trust_env=False,
        )
        self._executor = ProviderExecutionClient(
            base_url=config.core_url,
            provider_id=PROVIDER_ID,
            token=config.provider_token,
            timeout_seconds=5.0,
        )
        self._supervisor = StatusProcessSupervisor(
            config.descriptor_path.read_bytes(),
            implementation_hash=self._implementation.inventory_entry.implementation_hash,
        )
        self._registry = ProviderRegistryClient(
            base_url=config.core_url,
            provider_id=PROVIDER_ID,
            token=config.provider_token,
            tools=[self._implementation],
            max_concurrency=1,
            timeout_seconds=5.0,
        )
        self._inventory_hash: str | None = None

    async def __aenter__(self) -> "StatusFaultDriver":
        await self._admin.__aenter__()
        await self._executor.__aenter__()
        await self._registry.__aenter__()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._registry.aclose()
        await self._executor.aclose()
        await self._admin.aclose()

    async def _admin_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: int,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._admin.request(method, path, json=payload, headers=headers)
        if response.status_code != expected_status:
            raise DrillError(f"Core admin operation returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise DrillError("Core admin operation returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise DrillError("Core admin operation returned a non-object")
        return result

    async def _publish_runtime(self) -> None:
        inventory = self._registry.build_inventory()
        await self._registry.publish_inventory(inventory)
        self._inspector.inspect({"scope": "provider_runtime"})
        await self._registry.publish_heartbeat(
            self._registry.build_heartbeat(
                inventory_hash=inventory.snapshot_hash,
                health="healthy",
                current_concurrency=0,
                metadata={
                    "execution_enabled": True,
                    "role": "phase3_fault_driver",
                    "self_test": "ok",
                    "isolation": "spawn_hard_deadline",
                },
            )
        )
        self._inventory_hash = inventory.snapshot_hash

    async def _verify_authority(self) -> None:
        detail = await self._admin_json(
            "GET",
            f"/v1/tools/{TOOL_ID}",
            expected_status=200,
        )
        execution = detail.get("execution")
        if not isinstance(execution, dict):
            raise DrillError("Registry execution view is missing")
        active = execution.get("active_rollout_plan")
        if (
            execution.get("mode") != "canary"
            or execution.get("leases_enabled") is not True
            or not isinstance(active, dict)
            or active.get("plan_id") != self.config.expected_plan_id
            or active.get("version") != "1.0.0"
            or active.get("plan_hash") != self.config.expected_plan_hash
            or active.get("max_invocations") != 1
            or active.get("consumed_invocations") != 0
        ):
            raise DrillError("the exact reviewed rollout plan is not the sole active authority")
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
            or version.get("effective", {}).get("eligible") is not True
        ):
            raise DrillError("status.inspect@1.0.2 is not exactly active and eligible")

    async def _create_invocation(self) -> dict[str, Any]:
        payload = {
            "schema_version": "1.0",
            "tool_id": TOOL_ID,
            "descriptor_version": DESCRIPTOR_VERSION,
            "descriptor_hash": DESCRIPTOR_HASH,
            "input": {"scope": "provider_runtime"},
            "principal": {
                "platform": "qq",
                "sender_id": "phase3-operator",
                "conversation_id": CONVERSATION.removeprefix("qq:"),
                "conversation_type": "group",
                "platform_roles": ["operator"],
                "source_event_id": f"phase3:fault:{self.config.run_id}",
                "entry_id": f"phase3-fault-{self.config.run_id}",
            },
            "capabilities": [],
        }
        view = await self._admin_json(
            "POST",
            "/v1/tool-invocations",
            expected_status=201,
            payload=payload,
            headers={"Idempotency-Key": f"phase3-fault-{self.config.run_id}"},
        )
        rollout = view.get("policy", {}).get("rollout_plan")
        rollout_scope = view.get("policy", {}).get("rollout_scope")
        if (
            view.get("state") != "queued"
            or view.get("selected_provider_id") != PROVIDER_ID
            or not isinstance(rollout, dict)
            or rollout.get("plan_id") != self.config.expected_plan_id
            or rollout.get("version") != "1.0.0"
            or rollout.get("plan_hash") != self.config.expected_plan_hash
            or not isinstance(rollout_scope, dict)
            or rollout_scope.get("canonical_conversation") != CONVERSATION
            or rollout_scope.get("caller") != "admin_api"
            or rollout_scope.get("provider_id") != PROVIDER_ID
            or rollout_scope.get("expected_descriptor_resource_version") != 4
            or rollout_scope.get("expected_provider_resource_version") != 3
        ):
            raise DrillError("invocation did not freeze the exact active rollout authority")
        return view

    async def _view(self, invocation_id: str) -> dict[str, Any]:
        return await self._admin_json(
            "GET",
            f"/v1/tool-invocations/{invocation_id}",
            expected_status=200,
        )

    async def _wait_state(
        self,
        invocation_id: str,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> dict[str, Any]:
        stop = time.monotonic() + self.config.wait_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < stop:
            last = await self._view(invocation_id)
            if predicate(last):
                return last
            await asyncio.sleep(0.1)
        state = None if last is None else last.get("state")
        raise DrillError(f"invocation did not converge before timeout (last state {state})")

    async def _lease(self) -> ToolLeaseOut:
        if self._inventory_hash is None:
            raise DrillError("runtime inventory was not published")
        lease = await self._executor.request_lease(self._inventory_hash)
        if lease is None:
            raise DrillError("Core returned no lease for the exact active plan")
        if (
            lease.tool_id != TOOL_ID
            or lease.descriptor_version != DESCRIPTOR_VERSION
            or lease.descriptor_hash != DESCRIPTOR_HASH
            or lease.provider_id != PROVIDER_ID
        ):
            raise DrillError("Core returned a lease outside the reviewed drill target")
        return lease

    async def _run_worker(self, lease: ToolLeaseOut) -> SupervisedStatusResult:
        remaining = _remaining_seconds(lease) - 0.25
        if remaining < 0.75:
            raise DrillError("insufficient database-deadline margin for the bounded worker")
        result = await self._supervisor.execute(
            lease.input,
            timeout_seconds=min(remaining, 4.0),
        )
        if result.outcome != "success" or result.output is None:
            raise DrillError("bounded status worker did not return a valid success")
        return result

    async def _start(self, lease: ToolLeaseOut) -> None:
        await self._executor.start(
            lease.invocation_id,
            ToolExecutionStartIn.model_validate(_proof(lease)),
        )

    async def _complete(
        self,
        lease: ToolLeaseOut,
        result: SupervisedStatusResult,
        *,
        suffix: str,
    ) -> dict[str, Any]:
        if result.output is None:
            raise DrillError("bounded status result is missing its validated output")
        return await self._executor.complete(
            lease.invocation_id,
            ToolExecutionCompleteIn(
                **_proof(lease),
                provider_result_id=(
                    f"phase3:{self.config.scenario}:{self.config.run_id}:{suffix}"
                ),
                output=result.output,
                usage=result.usage,
            ),
        )

    @staticmethod
    async def _expect_conflict(operation: Awaitable[dict[str, Any]]) -> None:
        try:
            await operation
        except ProviderExecutionError as exc:
            if "HTTP 409" in str(exc):
                return
            raise DrillError("expected execution conflict had another bounded failure") from exc
        raise DrillError("stale or duplicate execution operation was unexpectedly accepted")

    async def _cancel(self, invocation_id: str, reason: str) -> dict[str, Any]:
        return await self._admin_json(
            "POST",
            f"/v1/tool-invocations/{invocation_id}/cancel",
            expected_status=200,
            payload={"schema_version": "1.0", "reason": reason},
        )

    async def _retry_fence_success(self, invocation: dict[str, Any]) -> dict[str, Any]:
        first = await self._lease()
        lease_seconds = (first.lease_expires_at - datetime.now(timezone.utc)).total_seconds()
        if not 0 < lease_seconds <= 1.5:
            raise DrillError("retry drill requires transient Core lease_seconds=1")
        await self._start(first)
        await self._wait_state(
            first.invocation_id,
            lambda view: view.get("state") == "queued"
            and len(view.get("attempts", [])) == 1
            and view["attempts"][0]["state"] == "lease_expired",
        )
        second = await self._lease()
        if (
            second.attempt_number != 2
            or second.fencing_token != first.fencing_token + 1
            or second.attempt_id == first.attempt_id
        ):
            raise DrillError("safe retry did not issue a new monotonic attempt/fence")
        await self._expect_conflict(
            self._executor.start(
                first.invocation_id,
                ToolExecutionStartIn.model_validate(_proof(first)),
            )
        )
        await self._start(second)
        result = await self._run_worker(second)
        receipt = await self._complete(second, result, suffix="success")
        if receipt.get("state") != "succeeded":
            raise DrillError("second fenced attempt did not succeed")
        await self._expect_conflict(self._complete(second, result, suffix="duplicate"))
        await self._expect_conflict(self._complete(first, result, suffix="stale"))
        return await self._view(invocation["invocation_id"])

    async def _invalid_output(self, invocation: dict[str, Any]) -> dict[str, Any]:
        lease = await self._lease()
        await self._start(lease)
        output = {"status": "ok"}
        input_bytes = len(canonicalize_json_value(lease.input).canonical_bytes)
        output_bytes = len(canonicalize_json_value(output).canonical_bytes)
        receipt = await self._executor.complete(
            lease.invocation_id,
            ToolExecutionCompleteIn(
                **_proof(lease),
                provider_result_id=f"phase3:invalid-output:{self.config.run_id}",
                output=output,
                usage=ToolUsage(
                    wall_time_ms=1,
                    input_bytes=input_bytes,
                    output_bytes=output_bytes,
                ),
            ),
        )
        if (
            receipt.get("state") != "failed"
            or receipt.get("error_code") != "invalid_output"
            or receipt.get("output") is not None
        ):
            raise DrillError("invalid output did not fail closed without storing a result")
        return await self._view(invocation["invocation_id"])

    async def _clock_skew_success(self, invocation: dict[str, Any]) -> dict[str, Any]:
        lease = await self._lease()
        await self._start(lease)
        for observed_at in (
            datetime(2099, 1, 1, tzinfo=timezone.utc),
            datetime(1970, 1, 1, tzinfo=timezone.utc),
        ):
            receipt = await self._executor.heartbeat(
                lease.invocation_id,
                ToolExecutionHeartbeatIn(
                    **_proof(lease),
                    usage=ToolUsage(wall_time_ms=1),
                    provider_observed_at=observed_at,
                ),
            )
            lease_expires_at = datetime.fromisoformat(str(receipt["lease_expires_at"]))
            if lease_expires_at > lease.deadline_at:
                raise DrillError("provider clock incorrectly extended the database deadline")
        result = await self._run_worker(lease)
        receipt = await self._complete(lease, result, suffix="success")
        if receipt.get("state") != "succeeded":
            raise DrillError("clock-skew diagnostic drill did not finish successfully")
        return await self._view(invocation["invocation_id"])

    async def _cancel_acknowledged(self, invocation: dict[str, Any]) -> dict[str, Any]:
        lease = await self._lease()
        await self._start(lease)
        cancelled = await self._cancel(
            lease.invocation_id,
            "Phase 3 drill: provider must acknowledge cancellation",
        )
        if cancelled.get("state") != "cancel_requested":
            raise DrillError("running cancellation was not recorded")
        heartbeat = await self._executor.heartbeat(
            lease.invocation_id,
            ToolExecutionHeartbeatIn(**_proof(lease), usage=ToolUsage(wall_time_ms=1)),
        )
        if heartbeat.get("cancel_requested") is not True:
            raise DrillError("Provider heartbeat did not observe cancellation")
        receipt = await self._executor.fail(
            lease.invocation_id,
            ToolExecutionFailIn(
                **_proof(lease),
                provider_result_id=f"phase3:cancel-ack:{self.config.run_id}",
                error_code="cancelled",
                safe_detail="status execution stopped after the drill cancellation request",
                usage=ToolUsage(wall_time_ms=1),
            ),
        )
        if receipt.get("state") != "cancelled":
            raise DrillError("Provider cancellation acknowledgement did not converge to cancelled")
        return await self._view(invocation["invocation_id"])

    async def _cancel_completion_race(self, invocation: dict[str, Any]) -> dict[str, Any]:
        lease = await self._lease()
        await self._start(lease)
        result = await self._run_worker(lease)
        cancelled = await self._cancel(
            lease.invocation_id,
            "Phase 3 drill: completion intentionally races cancellation",
        )
        if cancelled.get("state") != "cancel_requested":
            raise DrillError("cancellation race was not armed")
        receipt = await self._complete(lease, result, suffix="raced")
        if receipt.get("state") != "unknown_completion" or receipt.get("output") is not None:
            raise DrillError("completion race did not preserve explicit uncertainty")
        return await self._view(invocation["invocation_id"])

    async def _cancel_unacknowledged(self, invocation: dict[str, Any]) -> dict[str, Any]:
        lease = await self._lease()
        await self._start(lease)
        cancelled = await self._cancel(
            lease.invocation_id,
            "Phase 3 drill: provider intentionally withholds cancellation acknowledgement",
        )
        if cancelled.get("state") != "cancel_requested":
            raise DrillError("unacknowledged cancellation was not armed")
        return await self._wait_state(
            lease.invocation_id,
            lambda view: view.get("state") == "unknown_completion"
            and view.get("reason_code") == "cancellation_unacknowledged",
        )

    async def run(self) -> dict[str, Any]:
        await self._publish_runtime()
        await self._verify_authority()
        invocation = await self._create_invocation()
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "retry-fence-success": self._retry_fence_success,
            "invalid-output": self._invalid_output,
            "clock-skew-success": self._clock_skew_success,
            "cancel-acknowledged": self._cancel_acknowledged,
            "cancel-completion-race": self._cancel_completion_race,
            "cancel-unacknowledged": self._cancel_unacknowledged,
        }
        view = await handlers[self.config.scenario](invocation)
        return _safe_summary(
            view,
            scenario=self.config.scenario,
            plan_id=self.config.expected_plan_id,
            plan_hash=self.config.expected_plan_hash,
        )


def _parser() -> argparse.ArgumentParser:
    installed_descriptor = Path.cwd() / DEFAULT_DESCRIPTOR_PATH
    source_descriptor = _SOURCE_REPOSITORY_ROOT / DEFAULT_DESCRIPTOR_PATH
    default_descriptor = (
        installed_descriptor if installed_descriptor.is_file() else source_descriptor
    )
    parser = argparse.ArgumentParser(
        description="执行一次 Git-bound status.inspect Phase 3 故障演练",
    )
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("--expected-plan-id", required=True)
    parser.add_argument("--expected-plan-hash", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--descriptor",
        type=Path,
        default=default_descriptor,
    )
    parser.add_argument("--wait-seconds", type=float, default=8.0)
    parser.add_argument(
        "--provider-stopped-ack",
        action="store_true",
        help="确认常驻 status-provider 已停止，避免它抢走本次单 lease",
    )
    return parser


def _config(args: argparse.Namespace) -> DrillConfig:
    if not args.provider_stopped_ack:
        raise DrillError("refusing drill until the resident Provider stop is acknowledged")
    if not _RUN_ID_RE.fullmatch(args.run_id):
        raise DrillError("run-id must be 6-64 lowercase letters, digits or hyphens")
    if not re.fullmatch(r"[a-z][a-z0-9.-]{2,127}", args.expected_plan_id):
        raise DrillError("expected-plan-id is not a valid exact rollout plan ID")
    if not re.fullmatch(r"[0-9a-f]{64}", args.expected_plan_hash):
        raise DrillError("expected-plan-hash must be an exact lowercase SHA-256")
    if args.wait_seconds < 2 or args.wait_seconds > 30:
        raise DrillError("wait-seconds must be between 2 and 30")
    core_url = os.environ.pop("SUPERLILY_PHASE3_CORE_URL", "http://127.0.0.1:8765")
    admin_token = os.environ.pop("SUPERLILY_ADMIN_TOKEN", "")
    provider_token = os.environ.pop("SUPERLILY_STATUS_PROVIDER_TOKEN", "")
    parsed_url = urlsplit(core_url)
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname not in {"127.0.0.1", "localhost"}
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise DrillError("fault driver only accepts an explicit loopback HTTP Core URL")
    if (
        not admin_token
        or admin_token != admin_token.strip()
        or not provider_token
        or provider_token != provider_token.strip()
    ):
        raise DrillError("admin and status Provider credentials are required in the environment")
    return DrillConfig(
        core_url=core_url,
        admin_token=admin_token,
        provider_token=provider_token,
        descriptor_path=args.descriptor,
        scenario=args.scenario,
        expected_plan_id=args.expected_plan_id,
        expected_plan_hash=args.expected_plan_hash,
        run_id=args.run_id,
        wait_seconds=args.wait_seconds,
    )


async def _main(config: DrillConfig) -> dict[str, Any]:
    async with StatusFaultDriver(config) as driver:
        return await driver.run()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_main(_config(args)))
    except (DrillError, ProviderExecutionError) as exc:
        error = str(exc)
    except OSError:
        error = "local file or bounded worker operation failed"
    except httpx.HTTPError:
        error = "Core HTTP transport operation failed"
    except ValueError:
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
