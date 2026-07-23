"""``latex.render`` 的 Registry 上报、租约执行与 artifact 提交入口。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import logging
import os
from pathlib import Path
import re
import time
from typing import Any, Literal

from superlily_contracts import (
    LoadedToolDescriptor,
    ToolArtifactFinalizeIn,
    ToolArtifactReference,
    ToolArtifactReserveIn,
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
    MAX_ARTIFACT_BYTES,
    MAX_DIMENSION_PIXELS,
    MIME_TYPE,
    PROVIDER_ID,
    TOOL_ID,
    LatexPngResult,
    LatexWorkerClient,
    LatexWorkerError,
    latex_implementation_hash,
)


DEFAULT_DESCRIPTOR_PATH = Path("registry/descriptors/latex.render/1.0.0.json")
DEFAULT_SOCKET_PATH = Path("/latex-ipc/worker.sock")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
logger = logging.getLogger("superlily_latex_provider")


@dataclass(frozen=True, slots=True)
class LatexProviderConfig:
    core_url: str
    token: str = field(repr=False)
    worker_identity_hash: str
    descriptor_path: Path = DEFAULT_DESCRIPTOR_PATH
    worker_socket: Path = DEFAULT_SOCKET_PATH
    heartbeat_seconds: int = 30
    inventory_seconds: int = 300
    http_timeout_seconds: float = 8.0
    connect_timeout_seconds: float = 3.0
    poll_seconds: float = 0.25
    max_idle_poll_seconds: float = 1.0
    execution_heartbeat_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.core_url:
            raise ValueError("SUPERLILY_LATEX_PROVIDER_CORE_URL is required")
        if not self.token:
            raise ValueError("SUPERLILY_LATEX_PROVIDER_TOKEN is required")
        if not _SHA256_RE.fullmatch(self.worker_identity_hash):
            raise ValueError("worker identity must be a lowercase SHA-256")
        if not self.worker_socket.is_absolute():
            raise ValueError("latex worker socket must be absolute")
        if not 5 <= self.heartbeat_seconds <= 300:
            raise ValueError("latex provider heartbeat must be between 5 and 300 seconds")
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
    def from_env(cls) -> "LatexProviderConfig":
        return cls(
            core_url=os.getenv("SUPERLILY_LATEX_PROVIDER_CORE_URL", ""),
            token=os.getenv("SUPERLILY_LATEX_PROVIDER_TOKEN", ""),
            worker_identity_hash=os.getenv("SUPERLILY_LATEX_PROVIDER_WORKER_IDENTITY_HASH", ""),
            descriptor_path=Path(
                os.getenv("SUPERLILY_LATEX_PROVIDER_DESCRIPTOR_PATH", str(DEFAULT_DESCRIPTOR_PATH))
            ),
            worker_socket=Path(
                os.getenv("SUPERLILY_LATEX_PROVIDER_WORKER_SOCKET", str(DEFAULT_SOCKET_PATH))
            ),
            heartbeat_seconds=int(
                os.getenv("SUPERLILY_LATEX_PROVIDER_HEARTBEAT_SECONDS", "30")
            ),
            inventory_seconds=int(
                os.getenv("SUPERLILY_LATEX_PROVIDER_INVENTORY_SECONDS", "300")
            ),
            http_timeout_seconds=float(
                os.getenv("SUPERLILY_LATEX_PROVIDER_HTTP_TIMEOUT_SECONDS", "8")
            ),
            connect_timeout_seconds=float(
                os.getenv("SUPERLILY_LATEX_PROVIDER_CONNECT_TIMEOUT_SECONDS", "3")
            ),
            poll_seconds=float(os.getenv("SUPERLILY_LATEX_PROVIDER_POLL_SECONDS", "0.25")),
            max_idle_poll_seconds=float(
                os.getenv("SUPERLILY_LATEX_PROVIDER_MAX_IDLE_POLL_SECONDS", "1")
            ),
            execution_heartbeat_seconds=float(
                os.getenv("SUPERLILY_LATEX_PROVIDER_EXECUTION_HEARTBEAT_SECONDS", "1")
            ),
        )


@dataclass(frozen=True, slots=True)
class LatexExecutionResult:
    outcome: Literal["success", "failure"]
    usage: ToolUsage
    artifact: LatexPngResult | None = None
    error_code: Literal[
        "invalid_input",
        "timeout",
        "execution_failed",
        "invalid_output",
        "budget_exceeded",
        "internal_error",
    ] | None = None
    safe_detail: str | None = None


class LatexExecutor:
    """把精确 descriptor 绑定到无凭据 worker，并在 Provider 侧再次验图。"""

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
        policy = descriptor.artifact_policy
        if (
            descriptor.tool_id != TOOL_ID
            or descriptor.version != DESCRIPTOR_VERSION
            or descriptor.source_plugin != "superlily_latex_provider.runtime"
            or PROVIDER_ID not in descriptor.provider_selector.provider_ids
            or descriptor.retry_policy != "no_automatic_retry"
            or descriptor.natural_language
            or descriptor.execution_permissions.network != "deny"
            or descriptor.execution_permissions.filesystem != "sandbox_only"
            or descriptor.execution_permissions.subprocess != "sandbox_only"
            or descriptor.execution_permissions.remote_fetch != "deny"
            or descriptor.execution_permissions.artifacts != [MIME_TYPE]
            or descriptor.resource_budget.artifact_bytes != MAX_ARTIFACT_BYTES
            or policy is None
            or policy.max_count != 1
            or policy.max_single_bytes != MAX_ARTIFACT_BYTES
            or policy.max_width_pixels != MAX_DIMENSION_PIXELS
            or policy.max_height_pixels != MAX_DIMENSION_PIXELS
        ):
            raise ValueError("latex descriptor is not bound to the artifact sandbox")
        self.worker_identity_hash = worker_identity_hash
        self.implementation_hash = latex_implementation_hash(worker_identity_hash)
        self.worker = LatexWorkerClient(
            worker_socket, connect_timeout_seconds=connect_timeout_seconds
        )

    async def health(self) -> dict[str, Any]:
        return await self.worker.health()

    async def execute(
        self, payload: dict[str, Any], *, timeout_seconds: float
    ) -> LatexExecutionResult:
        started = time.monotonic()
        try:
            validate_schema_instance(payload, self.loaded.descriptor.input_schema)
        except (TypeError, ValueError):
            return LatexExecutionResult(
                outcome="failure",
                error_code="invalid_input",
                safe_detail="latex input failed its reviewed schema",
                usage=ToolUsage(wall_time_ms=max(0, int((time.monotonic() - started) * 1_000))),
            )
        try:
            input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
            if timeout_seconds < 1:
                raise LatexWorkerError("timeout", "latex execution has insufficient deadline")
            artifact = await asyncio.wait_for(
                self.worker.render(payload["latex"], timeout_seconds=timeout_seconds),
                timeout=timeout_seconds,
            )
            if len(artifact.content) > MAX_ARTIFACT_BYTES:
                raise LatexWorkerError("budget_exceeded", "latex artifact exceeded its descriptor budget")
            return LatexExecutionResult(
                outcome="success",
                artifact=artifact,
                usage=ToolUsage(
                    wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                    input_bytes=input_bytes,
                    artifact_bytes=len(artifact.content),
                ),
            )
        except TimeoutError:
            error = LatexWorkerError("timeout", "latex execution exceeded its hard deadline")
        except LatexWorkerError as exc:
            error = exc
        except (KeyError, TypeError, ValueError):
            error = LatexWorkerError("internal_error", "latex execution failed local checks")
        try:
            input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
        except Exception:
            input_bytes = 0
        return LatexExecutionResult(
            outcome="failure",
            error_code=error.error_code,
            safe_detail=error.safe_detail,
            usage=ToolUsage(
                wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                input_bytes=input_bytes,
            ),
        )


def _load_runtime(
    config: LatexProviderConfig, *, execution_enabled: bool
) -> tuple[LatexExecutor, ProviderToolImplementation]:
    descriptor_source = config.descriptor_path.read_bytes()
    executor = LatexExecutor(
        descriptor_source,
        worker_identity_hash=config.worker_identity_hash,
        worker_socket=config.worker_socket,
        connect_timeout_seconds=config.connect_timeout_seconds,
    )
    enforcement = {
        name: "hard" if execution_enabled else "unsupported"
        for name in ("wall_time", "memory", "input_bytes", "output_bytes", "artifact_bytes")
    }
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
        "isolation": "credentialless_no_network_unix_socket_worker",
        "worker_identity_hash": worker_identity_hash,
        "worker_status": None if health is None else health["status"],
        "worker_uid": None if health is None else health["uid"],
        "tex_version": None if health is None else health["tex_version"],
        "poppler_version": None if health is None else health["poppler_version"],
        "self_test": "ok" if failure is None else failure,
    }


async def _worker_health(executor: LatexExecutor) -> tuple[str, dict[str, Any] | None, str | None]:
    try:
        health = await executor.health()
    except Exception as exc:
        return "unavailable", None, f"failed:{type(exc).__name__}"
    return "healthy", health, None


async def run_reporter(config: LatexProviderConfig, *, once: bool = False) -> None:
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
        raise ValueError("Core lease does not match the exact latex implementation")


def _can_start(lease: ToolLeaseOut) -> bool:
    now = datetime.now(timezone.utc)
    return (
        (lease.lease_expires_at - now).total_seconds() > 0.5
        and (lease.deadline_at - now).total_seconds() > 0.75
    )


def _execution_seconds(lease: ToolLeaseOut, *, descriptor_timeout_ms: int) -> float:
    deadline_remaining = (lease.deadline_at - datetime.now(timezone.utc)).total_seconds() - 0.5
    return max(0.0, min(descriptor_timeout_ms / 1_000, deadline_remaining))


def _next_idle_poll_seconds(
    config: LatexProviderConfig, current: float, *, lease_received: bool
) -> float:
    if lease_received:
        return config.poll_seconds
    return min(config.max_idle_poll_seconds, max(config.poll_seconds, current * 2))


async def _cancel_execution(task: asyncio.Task[LatexExecutionResult]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _proof(lease: ToolLeaseOut) -> dict[str, Any]:
    return {
        "attempt_id": lease.attempt_id,
        "fencing_token": lease.fencing_token,
        "lease_secret": lease.lease_secret,
    }


async def _heartbeat(
    client: ProviderExecutionClient,
    lease: ToolLeaseOut,
    usage: ToolUsage,
) -> dict[str, Any]:
    return await client.heartbeat(
        lease.invocation_id,
        ToolExecutionHeartbeatIn(
            **_proof(lease),
            usage=usage,
            provider_observed_at=datetime.now(timezone.utc),
        ),
    )


async def _artifact_lease_keeper(
    client: ProviderExecutionClient,
    lease: ToolLeaseOut,
    usage: ToolUsage,
    interval_seconds: float,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass
        try:
            receipt = await _heartbeat(client, lease, usage)
        except ProviderExecutionError as exc:
            logger.warning("latex artifact heartbeat became unavailable or ambiguous: %s", exc)
            lost.set()
            return
        if receipt.get("cancel_requested") is True:
            lost.set()
            return


async def _store_artifact(
    client: ProviderExecutionClient,
    latex: LatexExecutor,
    lease: ToolLeaseOut,
    artifact: LatexPngResult,
    usage: ToolUsage,
    *,
    heartbeat_seconds: float,
) -> tuple[ToolArtifactReference, dict[str, Any]]:
    proof = _proof(lease)
    stop = asyncio.Event()
    lost = asyncio.Event()
    keeper = asyncio.create_task(
        _artifact_lease_keeper(client, lease, usage, heartbeat_seconds, stop, lost),
        name=f"latex-artifact-heartbeat-{lease.attempt_id}",
    )
    try:
        reservation = await client.reserve_artifact(
            lease.invocation_id,
            ToolArtifactReserveIn(
                **proof,
                mime_type=MIME_TYPE,
                declared_bytes=len(artifact.content),
                declared_sha256=artifact.content_sha256,
            ),
            idempotency_key=f"latex-artifact:{lease.attempt_id}:{lease.fencing_token}",
        )
        if lost.is_set():
            raise ProviderExecutionError("latex lease was lost after artifact reservation")
        uploaded = await client.upload_artifact(
            reservation.artifact_id,
            upload_secret=reservation.upload_secret,
            mime_type=MIME_TYPE,
            content=artifact.content,
        )
        if (
            uploaded.content_sha256 != artifact.content_sha256
            or uploaded.byte_size != len(artifact.content)
            or uploaded.width_pixels != artifact.width_pixels
            or uploaded.height_pixels != artifact.height_pixels
        ):
            raise ProviderExecutionError("Core artifact receipt did not match the local PNG")
        if lost.is_set():
            raise ProviderExecutionError("latex lease was lost after artifact upload")
        finalized = await client.finalize_artifact(
            lease.invocation_id,
            ToolArtifactFinalizeIn(
                **proof,
                **uploaded.model_dump(mode="json", exclude={"state"}),
            ),
        )
        output = {
            "kind": "image",
            "artifact_id": finalized.artifact_id,
            "mime_type": finalized.mime_type,
            "content_sha256": finalized.content_sha256,
            "byte_size": finalized.byte_size,
            "width_pixels": finalized.width_pixels,
            "height_pixels": finalized.height_pixels,
        }
        validate_schema_instance(output, latex.loaded.descriptor.output_schema)
        return finalized, output
    finally:
        stop.set()
        await keeper


async def _execute_lease(
    client: ProviderExecutionClient,
    latex: LatexExecutor,
    implementation: ProviderToolImplementation,
    lease: ToolLeaseOut,
    config: LatexProviderConfig,
    *,
    inventory_hash: str,
) -> None:
    try:
        _validate_lease_identity(lease, implementation, inventory_hash=inventory_hash)
    except ValueError as exc:
        logger.error("refusing mismatched latex lease: %s", exc)
        return
    if not _can_start(lease):
        logger.warning("refusing latex lease without enough initial start budget")
        return
    timeout_seconds = _execution_seconds(
        lease, descriptor_timeout_ms=implementation.loaded_descriptor.descriptor.timeout_ms
    )
    if timeout_seconds < 1:
        logger.warning("refusing latex lease without one second of execution budget")
        return
    proof = _proof(lease)
    try:
        await client.start(lease.invocation_id, ToolExecutionStartIn.model_validate(proof))
    except ProviderExecutionError as exc:
        logger.warning("latex start became unavailable or ambiguous: %s", exc)
        return
    execution = asyncio.create_task(
        latex.execute(lease.input, timeout_seconds=timeout_seconds),
        name=f"latex-render-{lease.attempt_id}",
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
                receipt = await _heartbeat(client, lease, live_usage)
            except ProviderExecutionError as exc:
                logger.warning("latex heartbeat became unavailable or ambiguous: %s", exc)
                await _cancel_execution(execution)
                return
            if receipt.get("cancel_requested") is True:
                await _cancel_execution(execution)
                return
    except asyncio.CancelledError:
        await _cancel_execution(execution)
        raise

    provider_result_id = f"latex:{lease.attempt_id}:{lease.fencing_token}"
    try:
        if result.outcome == "success" and result.artifact is not None:
            live_usage = ToolUsage(
                wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                input_bytes=input_bytes,
                artifact_bytes=len(result.artifact.content),
            )
            reference, output = await _store_artifact(
                client,
                latex,
                lease,
                result.artifact,
                live_usage,
                heartbeat_seconds=config.execution_heartbeat_seconds,
            )
            output_bytes = len(canonicalize_json_value(output).canonical_bytes)
            output_limit = implementation.loaded_descriptor.descriptor.resource_budget.output_bytes
            if output_limit is None or output_bytes > output_limit:
                raise ProviderExecutionError("latex output exceeded its descriptor budget")
            usage = ToolUsage(
                wall_time_ms=max(0, int((time.monotonic() - started) * 1_000)),
                input_bytes=input_bytes,
                output_bytes=output_bytes,
                artifact_bytes=len(result.artifact.content),
            )
            await client.complete(
                lease.invocation_id,
                ToolExecutionCompleteIn(
                    **proof,
                    provider_result_id=provider_result_id,
                    output=output,
                    usage=usage,
                    artifacts=[reference],
                ),
            )
        else:
            await client.fail(
                lease.invocation_id,
                ToolExecutionFailIn(
                    **proof,
                    provider_result_id=provider_result_id,
                    error_code=result.error_code or "internal_error",
                    safe_detail=result.safe_detail or "latex execution failed safely",
                    usage=result.usage,
                ),
            )
    except ProviderExecutionError as exc:
        logger.warning("latex artifact or completion became unavailable or ambiguous: %s", exc)


async def run_executor(config: LatexProviderConfig, *, once: bool = False) -> None:
    latex, implementation = _load_runtime(config, execution_enabled=True)
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
                health_state, health, failure = await _worker_health(latex)
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
                    logger.warning("latex lease request unavailable or ambiguous: %s", exc)
                    if once:
                        raise
                    lease = None
                if lease is not None:
                    lease_received = True
                    await _execute_lease(
                        executor,
                        latex,
                        implementation,
                        lease,
                        config,
                        inventory_hash=inventory_hash,
                    )
            if once:
                return
            idle_poll_seconds = _next_idle_poll_seconds(
                config, idle_poll_seconds, lease_received=lease_received
            )
            await asyncio.sleep(max(0.0, idle_poll_seconds - (time.monotonic() - loop_started)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-latex-provider")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="校验 descriptor 与 worker 身份绑定")
    verify.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    verify.add_argument("--worker-identity-hash", required=True)
    probe = subparsers.add_parser("probe", help="渲染固定公式但不上传 Core")
    probe.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    probe.add_argument("--worker-identity-hash", required=True)
    probe.add_argument("--worker-socket", type=Path, default=DEFAULT_SOCKET_PATH)
    subparsers.add_parser("report", help="只上报 inventory/heartbeat").add_argument(
        "--once", action="store_true"
    )
    subparsers.add_parser("serve", help="上报硬边界并领取精确租约").add_argument(
        "--once", action="store_true"
    )
    return parser


def _verify_config(args: argparse.Namespace) -> LatexProviderConfig:
    return LatexProviderConfig(
        core_url="http://127.0.0.1",
        token="verify-only",
        worker_identity_hash=args.worker_identity_hash,
        descriptor_path=args.descriptor,
        worker_socket=getattr(args, "worker_socket", DEFAULT_SOCKET_PATH),
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("SUPERLILY_LATEX_PROVIDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        if args.command == "verify":
            executor, implementation = _load_runtime(
                _verify_config(args), execution_enabled=True
            )
            print(
                canonicalize_json_value(
                    {
                        "descriptor_hash": implementation.loaded_descriptor.authority.sha256,
                        "implementation_hash": executor.implementation_hash,
                        "worker_identity_hash": executor.worker_identity_hash,
                    }
                ).canonical_bytes.decode("utf-8")
            )
        elif args.command == "probe":
            config = _verify_config(args)
            executor, _ = _load_runtime(config, execution_enabled=True)
            result = asyncio.run(
                executor.execute({"latex": "x^2+y^2=z^2"}, timeout_seconds=25)
            )
            if result.outcome != "success" or result.artifact is None:
                raise RuntimeError(result.safe_detail or "latex probe failed")
            print(
                canonicalize_json_value(
                    {
                        "byte_size": len(result.artifact.content),
                        "content_sha256": sha256(result.artifact.content).hexdigest(),
                        "height_pixels": result.artifact.height_pixels,
                        "mime_type": MIME_TYPE,
                        "width_pixels": result.artifact.width_pixels,
                    }
                ).canonical_bytes.decode("utf-8")
            )
        elif args.command == "report":
            config = LatexProviderConfig.from_env()
            os.environ.pop("SUPERLILY_LATEX_PROVIDER_TOKEN", None)
            asyncio.run(run_reporter(config, once=args.once))
        else:
            config = LatexProviderConfig.from_env()
            os.environ.pop("SUPERLILY_LATEX_PROVIDER_TOKEN", None)
            asyncio.run(run_executor(config, once=args.once))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("latex provider failed safely: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
