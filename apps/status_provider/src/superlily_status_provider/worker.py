"""无环境变量、无凭据的 ``status.inspect`` 独立执行进程。"""

from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import resource
import sys
from typing import Any

from superlily_contracts import load_tool_descriptor

from .status import StatusInspector


_MAX_REQUEST_BYTES = 131_072
_SAFE_ENVIRONMENT_KEYS = {"LC_CTYPE", "PYTHONPATH"}


def _environment_safe() -> bool:
    return set(os.environ).issubset(_SAFE_ENVIRONMENT_KEYS)


def _linux_peak_rss_bytes(status_source: str) -> int | None:
    """Read the post-exec process high-water mark from Linux procfs.

    ``ru_maxrss`` can retain the parent process high-water mark across the
    fork/exec path used by ``posix_spawn``.  ``VmHWM`` belongs to the new
    address space and therefore measures this worker rather than its Provider
    or a pytest parent.
    """

    for line in status_source.splitlines():
        name, separator, value = line.partition(":")
        if name != "VmHWM" or not separator:
            continue
        fields = value.split()
        if len(fields) != 2 or fields[1] != "kB" or not fields[0].isdigit():
            return None
        return int(fields[0]) * 1_024
    return None


def _usage() -> tuple[int, int]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu_ms = max(0, math.ceil((usage.ru_utime + usage.ru_stime) * 1_000))
    try:
        memory_peak_bytes = _linux_peak_rss_bytes(
            Path("/proc/self/status").read_text(encoding="ascii")
        )
    except (OSError, UnicodeError):
        memory_peak_bytes = None
    if memory_peak_bytes is None:
        # Linux 的 ru_maxrss 单位是 KiB；生产 Provider 镜像固定为 Linux。
        memory_peak_bytes = max(0, int(usage.ru_maxrss) * 1_024)
    return cpu_ms, memory_peak_bytes


def _failure(error_code: str = "execution_failed") -> dict[str, Any]:
    cpu_ms, memory_peak_bytes = _usage()
    return {
        "outcome": "failure",
        "error_code": error_code,
        "safe_detail": "status inspection failed safely",
        "cpu_ms": cpu_ms,
        "memory_peak_bytes": memory_peak_bytes,
        "environment_safe": _environment_safe(),
    }


def _execute(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != {
        "descriptor_base64",
        "implementation_hash",
        "payload",
    }:
        return _failure("invalid_request")
    descriptor_base64 = request["descriptor_base64"]
    implementation_hash = request["implementation_hash"]
    payload = request["payload"]
    if not isinstance(descriptor_base64, str) or not isinstance(implementation_hash, str):
        return _failure("invalid_request")
    if not isinstance(payload, dict):
        return _failure("invalid_request")
    try:
        descriptor_source = base64.b64decode(
            descriptor_base64.encode("ascii"),
            validate=True,
        )
        loaded = load_tool_descriptor(descriptor_source)
        inspector = StatusInspector(loaded, implementation_hash=implementation_hash)
        output = inspector.inspect(payload)
        cpu_ms, memory_peak_bytes = _usage()
        return {
            "outcome": "success",
            "output": output,
            "cpu_ms": cpu_ms,
            "memory_peak_bytes": memory_peak_bytes,
            "environment_safe": _environment_safe(),
        }
    except Exception:
        return _failure()


def main() -> int:
    try:
        source = sys.stdin.buffer.read(_MAX_REQUEST_BYTES + 1)
        if len(source) > _MAX_REQUEST_BYTES:
            message = _failure("request_too_large")
        else:
            message = _execute(json.loads(source))
    except Exception:
        message = _failure("invalid_request")
    encoded = json.dumps(
        message,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
