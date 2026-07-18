"""首个独立 Provider 的清单上报与 ``status.inspect`` 租约执行入口。"""

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
import time

from superlily_contracts import (
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolLeaseOut,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
)
from superlily_provider_sdk import (
    ProviderExecutionClient,
    ProviderExecutionError,
    ProviderRegistryClient,
    ProviderReportError,
    ProviderToolImplementation,
)

from .executor import StatusProcessSupervisor, SupervisedStatusResult
from .status import PROVIDER_ID, StatusInspector, status_implementation_hash


DEFAULT_DESCRIPTOR_PATH = Path("registry/descriptors/status.inspect/1.0.2.json")
logger = logging.getLogger("superlily_status_provider")


@dataclass(frozen=True, slots=True)
class StatusProviderConfig:
    core_url: str
    token: str = field(repr=False)
    descriptor_path: Path = DEFAULT_DESCRIPTOR_PATH
    heartbeat_seconds: int = 30
    inventory_seconds: int = 300
    timeout_seconds: float = 5.0
    poll_seconds: float = 0.25
    max_idle_poll_seconds: float = 5.0
    execution_heartbeat_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.core_url:
            raise ValueError("SUPERLILY_STATUS_PROVIDER_CORE_URL is required")
        if not self.token:
            raise ValueError("SUPERLILY_STATUS_PROVIDER_TOKEN is required")
        if not 5 <= self.heartbeat_seconds <= 300:
            raise ValueError("status provider heartbeat interval must be between 5 and 300 seconds")
        if not self.heartbeat_seconds <= self.inventory_seconds <= 86_400:
            raise ValueError("inventory interval must be at least the heartbeat interval")
        if self.timeout_seconds <= 0:
            raise ValueError("status provider timeout must be positive")
        if not 0.05 <= self.poll_seconds <= 5:
            raise ValueError("status provider poll interval must be between 0.05 and 5 seconds")
        if not self.poll_seconds <= self.max_idle_poll_seconds <= 60:
            raise ValueError(
                "status provider max idle poll interval must be between poll interval and 60 seconds"
            )
        if not 0.1 <= self.execution_heartbeat_seconds <= 5:
            raise ValueError(
                "execution heartbeat interval must be between 0.1 and 5 seconds"
            )

    @classmethod
    def from_env(cls) -> "StatusProviderConfig":
        return cls(
            core_url=os.getenv("SUPERLILY_STATUS_PROVIDER_CORE_URL", ""),
            token=os.getenv("SUPERLILY_STATUS_PROVIDER_TOKEN", ""),
            descriptor_path=Path(
                os.getenv(
                    "SUPERLILY_STATUS_PROVIDER_DESCRIPTOR_PATH",
                    str(DEFAULT_DESCRIPTOR_PATH),
                )
            ),
            heartbeat_seconds=int(
                os.getenv("SUPERLILY_STATUS_PROVIDER_HEARTBEAT_SECONDS", "30")
            ),
            inventory_seconds=int(
                os.getenv("SUPERLILY_STATUS_PROVIDER_INVENTORY_SECONDS", "300")
            ),
            timeout_seconds=float(
                os.getenv("SUPERLILY_STATUS_PROVIDER_TIMEOUT_SECONDS", "5")
            ),
            poll_seconds=float(
                os.getenv("SUPERLILY_STATUS_PROVIDER_POLL_SECONDS", "0.25")
            ),
            max_idle_poll_seconds=float(
                os.getenv("SUPERLILY_STATUS_PROVIDER_MAX_IDLE_POLL_SECONDS", "5")
            ),
            execution_heartbeat_seconds=float(
                os.getenv("SUPERLILY_STATUS_PROVIDER_EXECUTION_HEARTBEAT_SECONDS", "1")
            ),
        )


def _load_runtime(
    descriptor_path: Path,
    *,
    execution_enabled: bool = False,
) -> tuple[StatusInspector, ProviderToolImplementation]:
    descriptor_source = descriptor_path.read_bytes()
    loaded = load_tool_descriptor(descriptor_source)
    implementation_hash = status_implementation_hash()
    inspector = StatusInspector(loaded, implementation_hash=implementation_hash)
    implementation = ProviderToolImplementation.from_descriptor(
        descriptor_source,
        implementation_hash=implementation_hash,
        budget_enforcement={
            "output_bytes": "hard",
            "wall_time": "hard" if execution_enabled else "unsupported",
        },
    )
    return inspector, implementation


async def run_reporter(config: StatusProviderConfig, *, once: bool = False) -> None:
    inspector, implementation = _load_runtime(
        config.descriptor_path,
        execution_enabled=False,
    )
    client = ProviderRegistryClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        tools=[implementation],
        max_concurrency=implementation.loaded_descriptor.descriptor.concurrency_limit,
        timeout_seconds=config.timeout_seconds,
    )
    last_inventory_report = 0.0
    inventory_hash: str | None = None
    async with client:
        while True:
            loop_started = time.monotonic()
            if (
                inventory_hash is None
                or loop_started - last_inventory_report >= config.inventory_seconds
            ):
                inventory = client.build_inventory()
                try:
                    await client.publish_inventory(inventory)
                except ProviderReportError as exc:
                    logger.warning("inventory report unavailable: %s", exc)
                    if once:
                        raise
                else:
                    inventory_hash = inventory.snapshot_hash
                    last_inventory_report = loop_started

            if inventory_hash is not None:
                try:
                    inspector.inspect({"scope": "provider_runtime"})
                except Exception as exc:
                    health = "unavailable"
                    self_test = f"failed:{type(exc).__name__}"
                else:
                    health = "healthy"
                    self_test = "ok"
                heartbeat = client.build_heartbeat(
                    inventory_hash=inventory_hash,
                    health=health,
                    metadata={
                        "execution_enabled": False,
                        "role": "registry_reporter",
                        "self_test": self_test,
                    },
                )
                try:
                    await client.publish_heartbeat(heartbeat)
                except ProviderReportError as exc:
                    logger.warning("heartbeat report unavailable: %s", exc)
                    if once:
                        raise

            if once:
                return
            elapsed = time.monotonic() - loop_started
            await asyncio.sleep(max(0.0, config.heartbeat_seconds - elapsed))


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
        raise ValueError("Core lease does not match the exact reported implementation")


def _remaining_execution_seconds(lease: ToolLeaseOut, *, descriptor_timeout_ms: int) -> float:
    now = datetime.now(timezone.utc)
    absolute_stop = min(lease.deadline_at, lease.lease_expires_at)
    # 给 fail/complete 的网络往返留出固定余量；不足时宁可不启动。
    absolute_remaining = (absolute_stop - now).total_seconds() - 0.25
    return max(0.0, min(descriptor_timeout_ms / 1_000, absolute_remaining))


def _next_idle_poll_seconds(
    config: StatusProviderConfig,
    current: float,
    *,
    lease_received: bool,
) -> float:
    if lease_received:
        return config.poll_seconds
    return min(
        config.max_idle_poll_seconds,
        max(config.poll_seconds, current * 2),
    )


async def _cancel_supervisor(task: asyncio.Task[SupervisedStatusResult]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _execute_lease(
    client: ProviderExecutionClient,
    supervisor: StatusProcessSupervisor,
    implementation: ProviderToolImplementation,
    lease: ToolLeaseOut,
    config: StatusProviderConfig,
    *,
    inventory_hash: str,
) -> None:
    try:
        _validate_lease_identity(lease, implementation, inventory_hash=inventory_hash)
    except ValueError as exc:
        logger.error("refusing mismatched execution lease: %s", exc)
        return
    descriptor = implementation.loaded_descriptor.descriptor
    if _remaining_execution_seconds(
        lease,
        descriptor_timeout_ms=descriptor.timeout_ms,
    ) <= 0:
        logger.warning("refusing execution lease with no safe deadline budget")
        return
    proof = {
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.fencing_token,
        "lease_secret": lease.lease_secret,
    }
    try:
        await client.start(
            lease.invocation_id,
            ToolExecutionStartIn.model_validate(proof),
        )
    except ProviderExecutionError as exc:
        logger.warning("execution start became unavailable or ambiguous: %s", exc)
        return

    timeout_seconds = _remaining_execution_seconds(
        lease,
        descriptor_timeout_ms=descriptor.timeout_ms,
    )
    execution = asyncio.create_task(
        supervisor.execute(lease.input, timeout_seconds=timeout_seconds),
        name=f"status-inspect-{lease.attempt_id}",
    )
    started = time.monotonic()
    input_bytes = len(canonicalize_json_value(lease.input).canonical_bytes)
    try:
        while True:
            done, _ = await asyncio.wait(
                {execution},
                timeout=config.execution_heartbeat_seconds,
            )
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
                logger.warning("execution heartbeat became unavailable or ambiguous: %s", exc)
                await _cancel_supervisor(execution)
                return
            if receipt.get("cancel_requested") is True:
                await _cancel_supervisor(execution)
                try:
                    await client.fail(
                        lease.invocation_id,
                        ToolExecutionFailIn(
                            **proof,
                            provider_result_id=(
                                f"status:{lease.attempt_id}:{lease.fencing_token}:cancelled"
                            ),
                            error_code="cancelled",
                            safe_detail="status execution stopped after cancellation request",
                            usage=live_usage,
                        ),
                    )
                except ProviderExecutionError as exc:
                    logger.warning("cancellation acknowledgement became ambiguous: %s", exc)
                return
    except asyncio.CancelledError:
        await _cancel_supervisor(execution)
        raise

    provider_result_id = f"status:{lease.attempt_id}:{lease.fencing_token}"
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
                    safe_detail=(
                        result.safe_detail or "status execution failed without unsafe detail"
                    ),
                    usage=result.usage,
                ),
            )
    except ProviderExecutionError as exc:
        logger.warning("execution completion became unavailable or ambiguous: %s", exc)


async def run_executor(config: StatusProviderConfig, *, once: bool = False) -> None:
    descriptor_source = config.descriptor_path.read_bytes()
    inspector, implementation = _load_runtime(
        config.descriptor_path,
        execution_enabled=True,
    )
    supervisor = StatusProcessSupervisor(
        descriptor_source,
        implementation_hash=implementation.inventory_entry.implementation_hash,
    )
    registry = ProviderRegistryClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        tools=[implementation],
        # 当前实现串行领取；对 Core 诚实报告本 Provider 的本地容量为 1。
        max_concurrency=1,
        timeout_seconds=config.timeout_seconds,
    )
    executor = ProviderExecutionClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
    )
    last_inventory_report = 0.0
    last_heartbeat_report = 0.0
    inventory_hash: str | None = None
    idle_poll_seconds = config.poll_seconds
    async with registry, executor:
        while True:
            loop_started = time.monotonic()
            lease_received = False
            if (
                inventory_hash is None
                or loop_started - last_inventory_report >= config.inventory_seconds
            ):
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

            if inventory_hash is not None and (
                loop_started - last_heartbeat_report >= config.heartbeat_seconds
            ):
                try:
                    inspector.inspect({"scope": "provider_runtime"})
                except Exception as exc:
                    health = "unavailable"
                    self_test = f"failed:{type(exc).__name__}"
                else:
                    health = "healthy"
                    self_test = "ok"
                heartbeat = registry.build_heartbeat(
                    inventory_hash=inventory_hash,
                    health=health,
                    current_concurrency=0,
                    metadata={
                        "execution_enabled": True,
                        "role": "lease_executor",
                        "self_test": self_test,
                        "isolation": "spawn_hard_deadline",
                    },
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
                    logger.warning("lease request unavailable or ambiguous: %s", exc)
                    if once:
                        raise
                    lease = None
                if lease is not None:
                    lease_received = True
                    await _execute_lease(
                        executor,
                        supervisor,
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
            elapsed = time.monotonic() - loop_started
            await asyncio.sleep(max(0.0, idle_poll_seconds - elapsed))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-status-provider")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser(
        "verify", help="validate the reviewed descriptor and local implementation"
    )
    verify.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    report = subparsers.add_parser(
        "report", help="publish inventory and heartbeat without accepting execution"
    )
    report.add_argument("--once", action="store_true")
    serve = subparsers.add_parser(
        "serve", help="publish hard enforcement and execute exact status leases"
    )
    serve.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("SUPERLILY_STATUS_PROVIDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 空 lease 轮询是内部健康流量；错误仍由 Provider 自己以 warning 记录。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "verify":
            inspector, implementation = _load_runtime(args.descriptor)
            result = inspector.inspect({"scope": "provider_runtime"})
            print(
                json.dumps(
                    {
                        "descriptor_hash": inspector.loaded_descriptor.authority.sha256,
                        "implementation_hash": implementation.inventory_entry.implementation_hash,
                        "output": result,
                        "execution_enabled": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        elif args.command == "report":
            config = StatusProviderConfig.from_env()
            os.environ.pop("SUPERLILY_STATUS_PROVIDER_TOKEN", None)
            asyncio.run(run_reporter(config, once=args.once))
        else:
            config = StatusProviderConfig.from_env()
            # 后续 spawn 出来的工具子进程不能继承 Provider credential。
            os.environ.pop("SUPERLILY_STATUS_PROVIDER_TOKEN", None)
            asyncio.run(run_executor(config, once=args.once))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("status provider failed safely: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
