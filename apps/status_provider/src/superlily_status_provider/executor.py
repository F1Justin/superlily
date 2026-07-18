"""以空环境独立进程执行 ``status.inspect``，并由父进程强制边界。"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Literal

import superlily_contracts as contracts_package
from superlily_contracts import (
    LoadedToolDescriptor,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
    strict_json_loads,
    validate_schema_instance,
)

from .status import StatusInspector


_WORKER_MODULE = "superlily_status_provider.worker"
_MAX_WORKER_RESPONSE_BYTES = 65_536
_WORKER_PYTHONPATH = os.pathsep.join(
    sorted(
        {
            str(Path(__file__).resolve().parents[1]),
            str(Path(contracts_package.__file__).resolve().parents[1]),
        }
    )
)


@dataclass(frozen=True, slots=True)
class SupervisedStatusResult:
    outcome: Literal["success", "failure"]
    usage: ToolUsage
    output: dict[str, Any] | None = None
    error_code: Literal["timeout", "execution_failed", "internal_error"] | None = None
    safe_detail: str | None = None


class StatusProcessSupervisor:
    """单任务独立进程监督器；进程创建时没有 Provider 或平台环境变量。"""

    def __init__(self, descriptor_source: bytes, *, implementation_hash: str) -> None:
        self._descriptor_source = bytes(descriptor_source)
        self._loaded: LoadedToolDescriptor = load_tool_descriptor(self._descriptor_source)
        # 在领取 lease 前证明 descriptor/implementation 绑定；不把 credential 传给 worker。
        StatusInspector(self._loaded, implementation_hash=implementation_hash)
        self.implementation_hash = implementation_hash

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
        request = canonicalize_json_value(
            {
                "descriptor_base64": base64.b64encode(self._descriptor_source).decode("ascii"),
                "implementation_hash": self.implementation_hash,
                "payload": payload,
            }
        ).canonical_bytes
        process: asyncio.subprocess.Process | None = None
        started = time.monotonic()
        try:
            start_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    _WORKER_MODULE,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env={"PYTHONPATH": _WORKER_PYTHONPATH},
                )
            )
            try:
                process = await asyncio.shield(start_task)
            except asyncio.CancelledError:
                with suppress(Exception):
                    process = await start_task
                if process is not None:
                    await self._stop(process)
                raise
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                await self._stop(process)
                return self._failure(
                    payload,
                    wall_time_ms=math.ceil((time.monotonic() - started) * 1_000),
                    error_code="timeout",
                    safe_detail="status inspection exceeded its hard wall time",
                )
            try:
                message_source = await asyncio.wait_for(
                    self._exchange(process, request),
                    timeout=remaining,
                )
            except TimeoutError:
                await self._stop(process)
                return self._failure(
                    payload,
                    wall_time_ms=math.ceil((time.monotonic() - started) * 1_000),
                    error_code="timeout",
                    safe_detail="status inspection exceeded its hard wall time",
                )
            wall_time_ms = math.ceil((time.monotonic() - started) * 1_000)
            return self._decode_result(
                payload,
                input_bytes=input_bytes,
                wall_time_ms=wall_time_ms,
                message_source=message_source,
            )
        except asyncio.CancelledError:
            if process is not None:
                await self._stop(process)
            raise
        except Exception:
            if process is not None:
                await self._stop(process)
            return self._failure(
                payload,
                wall_time_ms=math.ceil((time.monotonic() - started) * 1_000),
                error_code="internal_error",
                safe_detail="status execution supervisor failed safely",
            )

    @staticmethod
    async def _exchange(process: asyncio.subprocess.Process, request: bytes) -> bytes:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("status worker pipes are unavailable")
        process.stdin.write(request)
        await process.stdin.drain()
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()

        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = _MAX_WORKER_RESPONSE_BYTES + 1 - total
            if remaining <= 0:
                raise ValueError("status worker response exceeds its hard transport limit")
            chunk = await process.stdout.read(min(8_192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError("status worker exited unsuccessfully")
        return b"".join(chunks)

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            await process.wait()
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.5)
        except TimeoutError:
            process.kill()
            await process.wait()

    def _decode_result(
        self,
        payload: dict[str, Any],
        *,
        input_bytes: int,
        wall_time_ms: int,
        message_source: bytes,
    ) -> SupervisedStatusResult:
        try:
            if len(message_source) > _MAX_WORKER_RESPONSE_BYTES:
                raise ValueError("worker response exceeds transport limit")
            message = strict_json_loads(message_source)
            if not isinstance(message, dict):
                raise TypeError("worker result must be an object")
            if message.get("environment_safe") is not True:
                raise ValueError("worker inherited an unsafe environment")
            if (
                type(message["cpu_ms"]) is not int
                or type(message["memory_peak_bytes"]) is not int
            ):
                raise TypeError("worker usage must use exact integers")
            cpu_ms = message["cpu_ms"]
            memory_peak_bytes = message["memory_peak_bytes"]
            if cpu_ms < 0 or memory_peak_bytes < 0:
                raise ValueError("negative worker usage")
            if message.get("outcome") == "success":
                output = message["output"]
                if not isinstance(output, dict):
                    raise ValueError("non-object status result")
                validate_schema_instance(output, self._loaded.descriptor.output_schema)
                output_bytes = len(canonicalize_json_value(output).canonical_bytes)
                output_limit = self._loaded.descriptor.resource_budget.output_bytes
                if output_limit is None or output_bytes > output_limit:
                    raise ValueError("output budget violation")
                return SupervisedStatusResult(
                    outcome="success",
                    output=output,
                    usage=ToolUsage(
                        wall_time_ms=wall_time_ms,
                        cpu_ms=cpu_ms,
                        memory_peak_bytes=memory_peak_bytes,
                        input_bytes=input_bytes,
                        output_bytes=output_bytes,
                    ),
                )
            if message.get("outcome") == "failure":
                return SupervisedStatusResult(
                    outcome="failure",
                    error_code="execution_failed",
                    safe_detail="status inspection failed inside its bounded child",
                    usage=ToolUsage(
                        wall_time_ms=wall_time_ms,
                        cpu_ms=cpu_ms,
                        memory_peak_bytes=memory_peak_bytes,
                        input_bytes=input_bytes,
                    ),
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
