"""在无凭据子进程中执行 ``status.inspect``，并由父进程强制截止时间。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import math
import multiprocessing
from multiprocessing.connection import Connection
import os
import resource
import time
from typing import Any, Literal

from superlily_contracts import (
    LoadedToolDescriptor,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
    validate_schema_instance,
)

from .status import StatusInspector


@dataclass(frozen=True, slots=True)
class SupervisedStatusResult:
    outcome: Literal["success", "failure"]
    usage: ToolUsage
    output: dict[str, Any] | None = None
    error_code: Literal["timeout", "execution_failed", "internal_error"] | None = None
    safe_detail: str | None = None


def _child_usage() -> tuple[int, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_ms = max(0, math.ceil((usage.ru_utime + usage.ru_stime) * 1_000))
    # Linux 的 ru_maxrss 单位是 KiB；本项目的 Provider 镜像固定为 Linux。
    memory_peak_bytes = max(0, int(usage.ru_maxrss) * 1_024)
    return cpu_ms, memory_peak_bytes


def _status_child(
    sender: Connection,
    descriptor_source: bytes,
    implementation_hash: str,
    payload: dict[str, Any],
) -> None:
    """子进程不接收 lease secret、Provider token 或任何平台发送能力。"""

    os.environ.clear()
    try:
        loaded = load_tool_descriptor(descriptor_source)
        inspector = StatusInspector(loaded, implementation_hash=implementation_hash)
        output = inspector.inspect(payload)
        cpu_ms, memory_peak_bytes = _child_usage()
        message: dict[str, Any] = {
            "outcome": "success",
            "output": output,
            "cpu_ms": cpu_ms,
            "memory_peak_bytes": memory_peak_bytes,
        }
    except Exception as exc:
        cpu_ms, memory_peak_bytes = _child_usage()
        message = {
            "outcome": "failure",
            "error_code": "execution_failed",
            "safe_detail": f"status inspection failed safely ({type(exc).__name__})",
            "cpu_ms": cpu_ms,
            "memory_peak_bytes": memory_peak_bytes,
        }
    try:
        sender.send(message)
    except (BrokenPipeError, EOFError, OSError):
        # 父进程已因超时或取消关闭管道；子进程不打印原始异常。
        pass
    finally:
        sender.close()


class StatusProcessSupervisor:
    """单任务 spawn 监督器；超时或取消时必须终止并回收子进程。"""

    def __init__(self, descriptor_source: bytes, *, implementation_hash: str) -> None:
        self._descriptor_source = bytes(descriptor_source)
        self._loaded: LoadedToolDescriptor = load_tool_descriptor(self._descriptor_source)
        # 构造一次可证明 descriptor/implementation 绑定，避免在领取 lease 后才发现错误。
        StatusInspector(self._loaded, implementation_hash=implementation_hash)
        self.implementation_hash = implementation_hash
        self._context = multiprocessing.get_context("spawn")

    async def execute(
        self,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> SupervisedStatusResult:
        if timeout_seconds <= 0:
            return self._failure(
                payload,
                wall_time_ms=0,
                error_code="timeout",
                safe_detail="execution deadline elapsed before the child could start",
            )
        input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
        receiver, sender = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_status_child,
            args=(sender, self._descriptor_source, self.implementation_hash, payload),
            name="superlily-status-inspect",
            daemon=True,
        )
        started = time.monotonic()
        try:
            start_task = asyncio.create_task(asyncio.to_thread(process.start))
            try:
                await asyncio.shield(start_task)
            except asyncio.CancelledError:
                # to_thread 本身不能被取消；必须等 spawn 完成后再终止，
                # 否则会在 process.pid 尚为空时漏出一个孤儿子进程。
                with suppress(Exception):
                    await start_task
                raise
            sender.close()
            while True:
                elapsed = time.monotonic() - started
                remaining = timeout_seconds - elapsed
                if remaining <= 0:
                    await self._stop(process)
                    return self._failure(
                        payload,
                        wall_time_ms=math.ceil(elapsed * 1_000),
                        error_code="timeout",
                        safe_detail="status inspection exceeded its hard wall time",
                    )
                if await asyncio.to_thread(receiver.poll, min(0.05, remaining)):
                    try:
                        message = receiver.recv()
                    except (EOFError, OSError):
                        message = None
                    await asyncio.to_thread(process.join, 0.2)
                    if process.is_alive():
                        await self._stop(process)
                    wall_time_ms = math.ceil((time.monotonic() - started) * 1_000)
                    return self._decode_result(
                        payload,
                        input_bytes=input_bytes,
                        wall_time_ms=wall_time_ms,
                        message=message,
                    )
                if not process.is_alive():
                    await asyncio.to_thread(process.join, 0.2)
                    return self._failure(
                        payload,
                        wall_time_ms=math.ceil((time.monotonic() - started) * 1_000),
                        error_code="execution_failed",
                        safe_detail="status inspection child exited without a result",
                    )
        except asyncio.CancelledError:
            await self._stop(process)
            raise
        except Exception:
            await self._stop(process)
            return self._failure(
                payload,
                wall_time_ms=math.ceil((time.monotonic() - started) * 1_000),
                error_code="internal_error",
                safe_detail="status execution supervisor failed safely",
            )
        finally:
            receiver.close()
            sender.close()

    async def _stop(self, process: multiprocessing.Process) -> None:
        if process.pid is None:
            return
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 0.5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 0.5)
        if not process.is_alive():
            await asyncio.to_thread(process.join, 0)

    def _decode_result(
        self,
        payload: dict[str, Any],
        *,
        input_bytes: int,
        wall_time_ms: int,
        message: Any,
    ) -> SupervisedStatusResult:
        if not isinstance(message, dict):
            return self._failure(
                payload,
                wall_time_ms=wall_time_ms,
                error_code="execution_failed",
                safe_detail="status inspection child returned no bounded result",
            )
        try:
            if (
                type(message["cpu_ms"]) is not int
                or type(message["memory_peak_bytes"]) is not int
            ):
                raise TypeError("child usage must use exact integers")
            cpu_ms = message["cpu_ms"]
            memory_peak_bytes = message["memory_peak_bytes"]
            if cpu_ms < 0 or memory_peak_bytes < 0:
                raise ValueError("negative child usage")
            if message.get("outcome") == "success":
                output = message["output"]
                if not isinstance(output, dict):
                    raise ValueError("non-object status result")
                validate_schema_instance(output, self._loaded.descriptor.output_schema)
                output_bytes = len(canonicalize_json_value(output).canonical_bytes)
                output_limit = self._loaded.descriptor.resource_budget.output_bytes
                if output_limit is None or output_bytes > output_limit:
                    raise ValueError("output budget violation")
                usage = ToolUsage(
                    wall_time_ms=wall_time_ms,
                    cpu_ms=cpu_ms,
                    memory_peak_bytes=memory_peak_bytes,
                    input_bytes=input_bytes,
                    output_bytes=output_bytes,
                )
                return SupervisedStatusResult(
                    outcome="success",
                    output=output,
                    usage=usage,
                )
            if message.get("outcome") == "failure":
                usage = ToolUsage(
                    wall_time_ms=wall_time_ms,
                    cpu_ms=cpu_ms,
                    memory_peak_bytes=memory_peak_bytes,
                    input_bytes=input_bytes,
                )
                return SupervisedStatusResult(
                    outcome="failure",
                    error_code="execution_failed",
                    safe_detail="status inspection failed inside its bounded child",
                    usage=usage,
                )
        except (KeyError, TypeError, ValueError):
            pass
        return self._failure(
            payload,
            wall_time_ms=wall_time_ms,
            error_code="internal_error",
            safe_detail="status inspection child returned an invalid bounded result",
        )

    @staticmethod
    def _failure(
        payload: dict[str, Any],
        *,
        wall_time_ms: int,
        error_code: Literal["timeout", "execution_failed", "internal_error"],
        safe_detail: str,
    ) -> SupervisedStatusResult:
        try:
            input_bytes = len(canonicalize_json_value(payload).canonical_bytes)
        except Exception:
            input_bytes = 0
        return SupervisedStatusResult(
            outcome="failure",
            error_code=error_code,
            safe_detail=safe_detail,
            usage=ToolUsage(
                wall_time_ms=max(0, wall_time_ms),
                input_bytes=input_bytes,
            ),
        )
