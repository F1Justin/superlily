"""文本模式 ``wolfram.run`` 的 Registry 上报与租约执行入口。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Literal

from superlily_contracts import (
    LoadedToolDescriptor,
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolLeaseOut,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
    validate_schema_instance,
)
from superlily_provider_sdk import (
    ProviderExecutionClient,
    ProviderExecutionError,
    ProviderRegistryClient,
    ProviderReportError,
    ProviderToolImplementation,
)

from .runtime import (
    DESCRIPTOR_VERSION,
    PROVIDER_ID,
    TOOL_ID,
    WolframWorkerClient,
    WolframWorkerError,
    wolfram_implementation_hash,
)


DEFAULT_DESCRIPTOR_PATH = Path("registry/descriptors/wolfram.run/1.0.0.json")
DEFAULT_SOCKET_PATH = Path("/wolfram-ipc/worker.sock")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger("superlily_wolfram_provider")


@dataclass(frozen=True, slots=True)
class WolframProviderConfig:
    core_url: str
    token: str = field(repr=False)
    worker_identity_hash: str
    descriptor_path: Path = DEFAULT_DESCRIPTOR_PATH
    worker_socket: Path = DEFAULT_SOCKET_PATH
    heartbeat_seconds: int = 30
    inventory_seconds: int = 300
    http_timeout_seconds: float = 5.0
    connect_timeout_seconds: float = 3.0
    poll_seconds: float = 0.25
    max_idle_poll_seconds: float = 5.0
    execution_heartbeat_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.core_url:
            raise ValueError("SUPERLILY_WOLFRAM_PROVIDER_CORE_URL is required")
        if not self.token:
            raise ValueError("SUPERLILY_WOLFRAM_PROVIDER_TOKEN is required")
        if not _SHA256_RE.fullmatch(self.worker_identity_hash):
            raise ValueError("worker identity must be a lowercase SHA-256")
        if not self.worker_socket.is_absolute():
            raise ValueError("wolfram worker socket must be absolute")
        if not 5 <= self.heartbeat_seconds <= 300:
            raise ValueError("wolfram provider heartbeat must be between 5 and 300 seconds")
        if not self.heartbeat_seconds <= self.inventory_seconds <= 86_400:
            raise ValueError("inventory interval must be at least the heartbeat interval")
        if self.http_timeout_seconds <= 0 or self.connect_timeout_seconds <= 0:
            raise ValueError("provider timeouts must be positive")
        if not 0.05 <= self.poll_seconds <= 5:
            raise ValueError("provider poll interval must be between 0.05 and 5 seconds")
        if not self.poll_seconds <= self.max_idle_poll_seconds <= 60:
            raise ValueError("provider idle poll maximum is invalid")
        if not 0.1 <= self.execution_heartbeat_seconds <= 5:
            raise ValueError("execution heartbeat must be between 0.1 and 5 seconds")

    @classmethod
    def from_env(cls) -> "WolframProviderConfig":
        return cls(
            core_url=os.getenv("SUPERLILY_WOLFRAM_PROVIDER_CORE_URL", ""),
            token=os.getenv("SUPERLILY_WOLFRAM_PROVIDER_TOKEN", ""),
            worker_identity_hash=os.getenv(
                "SUPERLILY_WOLFRAM_PROVIDER_WORKER_IDENTITY_HASH",
                "",
            ),
            descriptor_path=Path(
                os.getenv(
                    "SUPERLILY_WOLFRAM_PROVIDER_DESCRIPTOR_PATH",
                    str(DEFAULT_DESCRIPTOR_PATH),
                )
            ),
            worker_socket=Path(
                os.getenv(
                    "SUPERLILY_WOLFRAM_PROVIDER_WORKER_SOCKET",
                    str(DEFAULT_SOCKET_PATH),
                )
            ),
            heartbeat_seconds=int(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_HEARTBEAT_SECONDS", "30")
            ),
            inventory_seconds=int(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_INVENTORY_SECONDS", "300")
            ),
            http_timeout_seconds=float(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_HTTP_TIMEOUT_SECONDS", "5")
            ),
            connect_timeout_seconds=float(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_CONNECT_TIMEOUT_SECONDS", "3")
            ),
            poll_seconds=float(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_POLL_SECONDS", "0.25")
            ),
            max_idle_poll_seconds=float(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_MAX_IDLE_POLL_SECONDS", "5")
            ),
            execution_heartbeat_seconds=float(
                os.getenv("SUPERLILY_WOLFRAM_PROVIDER_EXECUTION_HEARTBEAT_SECONDS", "1")
            ),
        )


@dataclass(frozen=True, slots=True)
class WolframExecutionResult:
    outcome: Literal["success", "failure"]
    usage: ToolUsage
    output: dict[str, Any] | None = None
    error_code: Literal["timeout", "execution_failed", "invalid_output", "internal_error"] | None = None
    safe_detail: str | None = None


class WolframExecutor:
    """将精确 descriptor 绑定到私有 worker socket，并独立校验结果。"""

    def __init__(
        self,
        descriptor_source: bytes,
        *,
        worker_identity_hash: str,
        worker_socket: Path,
        connect_timeout_seconds: float,
    ) -> None:
        self.descriptor_source = bytes(descriptor_source)
        self.loaded: LoadedToolDescriptor = load_tool_descriptor(self.descriptor_source)
        descriptor = self.loaded.descriptor
        if (
            descriptor.tool_id != TOOL_ID
            or descriptor.version != DESCRIPTOR_VERSION
            or descriptor.source_plugin != "superlily_wolfram_provider.runtime"
            or PROVIDER_ID not in descriptor.provider_selector.provider_ids
            or descriptor.retry_policy != "no_automatic_retry"
            or descriptor.natural_language
            or descriptor.execution_permissions.network != "deny"
            or descriptor.execution_permissions.filesystem != "sandbox_only"
            or descriptor.execution_permissions.subprocess != "sandbox_only"
            or descriptor.execution_permissions.remote_fetch != "deny"
            or descriptor.execution_permissions.artifacts
        ):
            raise ValueError("wolfram descriptor is not bound to the text-only sandbox")
        self.worker_identity_hash = worker_identity_hash
        self.implementation_hash = wolfram_implementation_hash(worker_identity_hash)
        self.worker = WolframWorkerClient(
            worker_socket,
            connect_timeout_seconds=connect_timeout_seconds,
        )

    async def health(self) -> dict[str, Any]:
        return await self.worker.health()

    async def execute(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> WolframExecutionResult:
        started = time.monotonic()
        try:
            validate_schema_instance(payload, self.loaded.descriptor.input_schema)
            input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
            if timeout_seconds < 1:
                raise WolframWorkerError(
                    "timeout",
                    "wolfram execution has less than one second before its deadline",
                )
            worker_result = await asyncio.wait_for(
                self.worker.evaluate(
                    payload["expression"],
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
            output = {"kind": "text", "text": worker_result.text}
            validate_schema_instance(output, self.loaded.descriptor.output_schema)
            output_bytes = len(canonicalize_json_value(output).canonical_bytes)
            output_limit = self.loaded.descriptor.resource_budget.output_bytes
            if output_limit is None or output_bytes > output_limit:
                raise WolframWorkerError(
                    "invalid_output",
                    "wolfram output exceeded its descriptor byte budget",
                )
            return WolframExecutionResult(
                outcome="success",
                output=output,
                usage=ToolUsage(
                    wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                    input_bytes=input_bytes,
                    output_bytes=output_bytes,
                ),
            )
        except TimeoutError:
            error = WolframWorkerError("timeout", "wolfram execution exceeded its hard deadline")
        except WolframWorkerError as exc:
            error = exc
        except (KeyError, TypeError, ValueError):
            error = WolframWorkerError(
                "internal_error",
                "wolfram execution failed its local contract checks",
            )
        try:
            input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
        except Exception:
            input_bytes = 0
        return WolframExecutionResult(
            outcome="failure",
            error_code=error.error_code,
            safe_detail=error.safe_detail,
            usage=ToolUsage(
                wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                input_bytes=input_bytes,
            ),
        )


def _load_runtime(
    config: WolframProviderConfig,
    *,
    execution_enabled: bool,
) -> tuple[WolframExecutor, ProviderToolImplementation]:
    descriptor_source = config.descriptor_path.read_bytes()
    executor = WolframExecutor(
        descriptor_source,
        worker_identity_hash=config.worker_identity_hash,
        worker_socket=config.worker_socket,
        connect_timeout_seconds=config.connect_timeout_seconds,
    )
    enforcement = (
        {
            "wall_time": "hard",
            "memory": "hard",
            "input_bytes": "hard",
            "output_bytes": "hard",
        }
        if execution_enabled
        else {
            "wall_time": "unsupported",
            "memory": "unsupported",
            "input_bytes": "unsupported",
            "output_bytes": "unsupported",
        }
    )
    implementation = ProviderToolImplementation.from_descriptor(
        descriptor_source,
        implementation_hash=executor.implementation_hash,
        budget_enforcement=enforcement,
    )
    return executor, implementation


def _heartbeat_metadata(
    *,
    execution_enabled: bool,
    worker_identity_hash: str,
    health: dict[str, Any] | None,
    failure: str | None,
) -> dict[str, Any]:
    return {
        "execution_enabled": execution_enabled,
        "role": "lease_executor" if execution_enabled else "registry_reporter",
        "isolation": "persistent_unix_socket_worker",
        "worker_identity_hash": worker_identity_hash,
        "worker_status": None if health is None else health["status"],
        "worker_uid": None if health is None else health["uid"],
        "self_test": "ok" if failure is None else failure,
    }


async def _worker_health(executor: WolframExecutor) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        health = await executor.health()
    except Exception as exc:
        return "unavailable", None, f"failed:{type(exc).__name__}"
    return "healthy", health, None


async def run_reporter(config: WolframProviderConfig, *, once: bool = False) -> None:
    executor, implementation = _load_runtime(config, execution_enabled=False)
    registry = ProviderRegistryClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        tools=[implementation],
        max_concurrency=1,
        timeout_seconds=config.http_timeout_seconds,
    )
    last_inventory_report = 0.0
    inventory_hash: str | None = None
    async with registry:
        while True:
            loop_started = time.monotonic()
            if inventory_hash is None or loop_started - last_inventory_report >= config.inventory_seconds:
                inventory = registry.build_inventory()
                try:
                    await registry.publish_inventory(inventory)
                except ProviderReportError as exc:
                    logger.warning("inventory report unavailable: %s", exc)
                    if once:
                        raise
                else:
                    inventory_hash = inventory.snapshot_hash
                    last_inventory_report = loop_started
            if inventory_hash is not None:
                health_state, health, failure = await _worker_health(executor)
                heartbeat = registry.build_heartbeat(
                    inventory_hash=inventory_hash,
                    health=health_state,
                    metadata=_heartbeat_metadata(
                        execution_enabled=False,
                        worker_identity_hash=config.worker_identity_hash,
                        health=health,
                        failure=failure,
                    ),
                )
                try:
                    await registry.publish_heartbeat(heartbeat)
                except ProviderReportError as exc:
                    logger.warning("heartbeat report unavailable: %s", exc)
                    if once:
                        raise
            if once:
                return
            await asyncio.sleep(max(0.0, config.heartbeat_seconds - (time.monotonic() - loop_started)))


def _validate_lease_identity(
    lease: ToolLeaseOut,
    implementation: ProviderToolImplementation,
    *,
    inventory_hash: str,
) -> None:
    descriptor = implementation.loaded_descriptor.descriptor
    entry = implementation.inventory_entry
    if (
        lease.provider_id != PROVIDER_ID
        or lease.inventory_hash != inventory_hash
        or lease.implementation_hash != entry.implementation_hash
        or lease.tool_id != descriptor.tool_id
        or lease.descriptor_version != descriptor.version
        or lease.descriptor_hash != entry.descriptor_hash
        or lease.resource_budget != descriptor.resource_budget
        or lease.execution_permissions != descriptor.execution_permissions
    ):
        raise ValueError("Core lease does not match the exact wolfram implementation")


def _can_start(lease: ToolLeaseOut) -> bool:
    now = datetime.now(timezone.utc)
    return (
        (lease.lease_expires_at - now).total_seconds() > 0.5
        and (lease.deadline_at - now).total_seconds() > 0.75
    )


def _execution_seconds(lease: ToolLeaseOut, *, descriptor_timeout_ms: int) -> float:
    # start 成功后靠 heartbeat 延长 lease；本地硬边界取 descriptor 与绝对 deadline
    # 的较小值，不能继续沿用 status 工具的“初始 lease expiry 即总时限”。
    deadline_remaining = (lease.deadline_at - datetime.now(timezone.utc)).total_seconds() - 0.5
    return max(0.0, min(descriptor_timeout_ms / 1_000, deadline_remaining))


def _next_idle_poll_seconds(config: WolframProviderConfig, current: float, *, lease_received: bool) -> float:
    if lease_received:
        return config.poll_seconds
    return min(config.max_idle_poll_seconds, max(config.poll_seconds, current * 2))


async def _cancel_execution(task: asyncio.Task[WolframExecutionResult]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _execute_lease(
    client: ProviderExecutionClient,
    wolfram: WolframExecutor,
    implementation: ProviderToolImplementation,
    lease: ToolLeaseOut,
    config: WolframProviderConfig,
    *,
    inventory_hash: str,
) -> None:
    try:
        _validate_lease_identity(lease, implementation, inventory_hash=inventory_hash)
    except ValueError as exc:
        logger.error("refusing mismatched wolfram lease: %s", exc)
        return
    if not _can_start(lease):
        logger.warning("refusing wolfram lease without enough initial start budget")
        return
    timeout_seconds = _execution_seconds(
        lease,
        descriptor_timeout_ms=implementation.loaded_descriptor.descriptor.timeout_ms,
    )
    if timeout_seconds < 1:
        logger.warning("refusing wolfram lease without one second of execution budget")
        return
    proof = {
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.fencing_token,
        "lease_secret": lease.lease_secret,
    }
    try:
        await client.start(lease.invocation_id, ToolExecutionStartIn.model_validate(proof))
    except ProviderExecutionError as exc:
        logger.warning("wolfram start became unavailable or ambiguous: %s", exc)
        return
    execution = asyncio.create_task(
        wolfram.execute(lease.input, timeout_seconds=timeout_seconds),
        name=f"wolfram-run-{lease.attempt_id}",
    )
    started = time.monotonic()
    input_bytes = len(canonicalize_json_value(lease.input).canonical_bytes)
    try:
        while True:
            done, _ = await asyncio.wait({execution}, timeout=config.execution_heartbeat_seconds)
            if execution in done:
                result = await execution
                break
            live_usage = ToolUsage(
                wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                input_bytes=input_bytes,
            )
            try:
                receipt = await client.heartbeat(
                    lease.invocation_id,
                    ToolExecutionHeartbeatIn(
                        **proof,
                        usage=live_usage,
                        provider_observed_at=datetime.now(timezone.utc),
                    ),
                )
            except ProviderExecutionError as exc:
                logger.warning("wolfram heartbeat became unavailable or ambiguous: %s", exc)
                await _cancel_execution(execution)
                return
            if receipt.get("cancel_requested") is True:
                # 旧 socket 协议只能断开客户端，不能证明内核已经停止。停止续租并让
                # Core 保守收敛为 unknown_completion，不能伪造 cancelled ACK。
                logger.warning("wolfram cancellation cannot be acknowledged by the legacy worker")
                await _cancel_execution(execution)
                return
    except asyncio.CancelledError:
        await _cancel_execution(execution)
        raise

    provider_result_id = f"wolfram:{lease.attempt_id}:{lease.fencing_token}"
    try:
        if result.outcome == "success" and result.output is not None:
            await client.complete(
                lease.invocation_id,
                ToolExecutionCompleteIn(
                    **proof,
                    provider_result_id=provider_result_id,
                    output=result.output,
                    usage=result.usage,
                ),
            )
        else:
            await client.fail(
                lease.invocation_id,
                ToolExecutionFailIn(
                    **proof,
                    provider_result_id=provider_result_id,
                    error_code=result.error_code or "internal_error",
                    safe_detail=result.safe_detail or "wolfram execution failed safely",
                    usage=result.usage,
                ),
            )
    except ProviderExecutionError as exc:
        logger.warning("wolfram completion became unavailable or ambiguous: %s", exc)


async def run_executor(config: WolframProviderConfig, *, once: bool = False) -> None:
    wolfram, implementation = _load_runtime(config, execution_enabled=True)
    registry = ProviderRegistryClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        tools=[implementation],
        max_concurrency=1,
        timeout_seconds=config.http_timeout_seconds,
    )
    executor = ProviderExecutionClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        timeout_seconds=config.http_timeout_seconds,
    )
    last_inventory_report = 0.0
    last_heartbeat_report = 0.0
    inventory_hash: str | None = None
    idle_poll_seconds = config.poll_seconds
    async with registry, executor:
        while True:
            loop_started = time.monotonic()
            lease_received = False
            if inventory_hash is None or loop_started - last_inventory_report >= config.inventory_seconds:
                inventory = registry.build_inventory()
                try:
                    await registry.publish_inventory(inventory)
                except ProviderReportError as exc:
                    logger.warning("executor inventory report unavailable: %s", exc)
                    if once:
                        raise
                else:
                    inventory_hash = inventory.snapshot_hash
                    last_inventory_report = loop_started
            if inventory_hash is not None and loop_started - last_heartbeat_report >= config.heartbeat_seconds:
                health_state, health, failure = await _worker_health(wolfram)
                heartbeat = registry.build_heartbeat(
                    inventory_hash=inventory_hash,
                    health=health_state,
                    current_concurrency=0,
                    metadata=_heartbeat_metadata(
                        execution_enabled=True,
                        worker_identity_hash=config.worker_identity_hash,
                        health=health,
                        failure=failure,
                    ),
                )
                try:
                    await registry.publish_heartbeat(heartbeat)
                except ProviderReportError as exc:
                    logger.warning("executor heartbeat report unavailable: %s", exc)
                    if once:
                        raise
                else:
                    last_heartbeat_report = loop_started
            if inventory_hash is not None:
                try:
                    lease = await executor.request_lease(inventory_hash)
                except ProviderExecutionError as exc:
                    logger.warning("wolfram lease request unavailable or ambiguous: %s", exc)
                    if once:
                        raise
                    lease = None
                if lease is not None:
                    lease_received = True
                    await _execute_lease(
                        executor,
                        wolfram,
                        implementation,
                        lease,
                        config,
                        inventory_hash=inventory_hash,
                    )
            if once:
                return
            idle_poll_seconds = _next_idle_poll_seconds(
                config,
                idle_poll_seconds,
                lease_received=lease_received,
            )
            await asyncio.sleep(max(0.0, idle_poll_seconds - (time.monotonic() - loop_started)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-wolfram-provider")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="校验 descriptor 与后端身份绑定")
    verify.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    verify.add_argument("--worker-identity-hash", required=True)
    probe = subparsers.add_parser("probe", help="执行固定 2+2 文本探针")
    probe.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    probe.add_argument("--worker-identity-hash", required=True)
    probe.add_argument("--worker-socket", type=Path, default=DEFAULT_SOCKET_PATH)
    report = subparsers.add_parser("report", help="只上报 inventory/heartbeat")
    report.add_argument("--once", action="store_true")
    serve = subparsers.add_parser("serve", help="上报硬边界并领取精确租约")
    serve.add_argument("--once", action="store_true")
    return parser


def _verify_config(args: argparse.Namespace) -> WolframProviderConfig:
    return WolframProviderConfig(
        core_url="http://127.0.0.1",
        token="verify-only",
        worker_identity_hash=args.worker_identity_hash,
        descriptor_path=args.descriptor,
        worker_socket=getattr(args, "worker_socket", DEFAULT_SOCKET_PATH),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("SUPERLILY_WOLFRAM_PROVIDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command in {"verify", "probe"}:
            config = _verify_config(args)
            executor, implementation = _load_runtime(config, execution_enabled=True)
            result: dict[str, Any] = {
                "descriptor_hash": executor.loaded.authority.sha256,
                "implementation_hash": implementation.inventory_entry.implementation_hash,
                "worker_identity_hash": config.worker_identity_hash,
                "execution_enabled": True,
            }
            if args.command == "probe":
                async def probe() -> dict[str, Any]:
                    health = await executor.health()
                    calculation = await executor.execute(
                        {"expression": "2+2"},
                        timeout_seconds=10,
                    )
                    return {
                        "health": health,
                        "outcome": calculation.outcome,
                        "output": calculation.output,
                    }

                result["probe"] = asyncio.run(probe())
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
        elif args.command == "report":
            config = WolframProviderConfig.from_env()
            os.environ.pop("SUPERLILY_WOLFRAM_PROVIDER_TOKEN", None)
            asyncio.run(run_reporter(config, once=args.once))
        else:
            config = WolframProviderConfig.from_env()
            os.environ.pop("SUPERLILY_WOLFRAM_PROVIDER_TOKEN", None)
            asyncio.run(run_executor(config, once=args.once))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("wolfram provider failed safely: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
