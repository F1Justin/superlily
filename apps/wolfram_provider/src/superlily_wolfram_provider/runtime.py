"""通过私有 Unix socket 调用现有持久 Wolfram worker。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
from typing import Any, Literal

from superlily_contracts import canonicalize_json_value, strict_json_loads


PROVIDER_ID = "provider-wolfram-primary"
TOOL_ID = "wolfram.run"
DESCRIPTOR_VERSION = "1.0.0"
MAX_EXPRESSION_BYTES = 8 * 1024
MAX_RESPONSE_BYTES = 32 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENGINE_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class WolframWorkerError(RuntimeError):
    """不包含表达式、worker 原始错误或本地路径的有界失败。"""

    def __init__(
        self,
        error_code: Literal["timeout", "execution_failed", "invalid_output", "internal_error"],
        safe_detail: str,
    ) -> None:
        super().__init__(safe_detail)
        self.error_code = error_code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class WolframTextResult:
    text: str


def build_worker_identity_hash(
    *,
    image_id: str,
    server_sha256: str,
    kernel_wrapper_sha256: str,
    engine_version: str,
    sandbox_profile_sha256: str,
) -> str:
    """规范化 worker 镜像、协议实现、引擎版本与隔离配置，生成部署身份。"""

    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise ValueError("worker image ID must be an exact Docker SHA-256")
    if not all(
        _SHA256_RE.fullmatch(value)
        for value in (server_sha256, kernel_wrapper_sha256, sandbox_profile_sha256)
    ):
        raise ValueError("worker source and sandbox hashes must be lowercase SHA-256 values")
    if not _ENGINE_VERSION_RE.fullmatch(engine_version):
        raise ValueError("worker engine version must use numeric major.minor.patch")
    return canonicalize_json_value(
        {
            "engine_version": engine_version,
            "image_id": image_id,
            "kernel_wrapper_sha256": kernel_wrapper_sha256,
            "sandbox_profile_sha256": sandbox_profile_sha256,
            "schema_version": "1.0",
            "server_sha256": server_sha256,
            "transport": "lily-wolfram-unix-json-line-v1",
        }
    ).sha256


def wolfram_implementation_hash(worker_identity_hash: str) -> str:
    """绑定 Provider 源码和显式部署的 worker 后端身份。"""

    if not _SHA256_RE.fullmatch(worker_identity_hash):
        raise ValueError("worker_identity_hash must be a lowercase SHA-256")
    digest = sha256()
    for name in ("main.py", "runtime.py"):
        source = Path(__file__).with_name(name).read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name.encode("utf-8"))
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    label = b"wolfram-worker-identity-v1"
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(bytes.fromhex(worker_identity_hash))
    return digest.hexdigest()


class WolframWorkerClient:
    """一次请求一个连接；只接受文本结果，不执行客户端重试。"""

    def __init__(
        self,
        socket_path: Path,
        *,
        connect_timeout_seconds: float = 3.0,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("worker socket path must be absolute")
        if connect_timeout_seconds <= 0:
            raise ValueError("worker connect timeout must be positive")
        self.socket_path = socket_path
        self.connect_timeout_seconds = connect_timeout_seconds

    def validate_socket_authority(self) -> None:
        """拒绝软链接、宽权限或不属于当前 Provider uid 的 socket。"""

        try:
            parent = self.socket_path.parent.lstat()
            endpoint = self.socket_path.lstat()
        except OSError as exc:
            raise WolframWorkerError(
                "internal_error",
                "wolfram worker socket is unavailable",
            ) from exc
        uid = os.getuid()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise WolframWorkerError(
                "internal_error",
                "wolfram worker socket directory failed authority checks",
            )
        if (
            not stat.S_ISSOCK(endpoint.st_mode)
            or stat.S_ISLNK(endpoint.st_mode)
            or endpoint.st_uid != uid
            or stat.S_IMODE(endpoint.st_mode) != 0o600
        ):
            raise WolframWorkerError(
                "internal_error",
                "wolfram worker socket failed authority checks",
            )

    async def health(self) -> dict[str, Any]:
        response = await self._call({"op": "health"}, timeout_seconds=5.0)
        if (
            set(response) != {"ok", "status", "requests", "uid", "pid"}
            or response.get("ok") is not True
            or response.get("status") != "ready"
            or type(response.get("requests")) is not int
            or response["requests"] < 0
            or response.get("uid") != os.getuid()
            or type(response.get("pid")) is not int
            or response["pid"] <= 0
        ):
            raise WolframWorkerError(
                "internal_error",
                "wolfram worker returned an invalid health response",
            )
        return {
            "status": "ready",
            "requests": response["requests"],
            "uid": response["uid"],
        }

    async def evaluate(self, expression: str, *, timeout_seconds: float) -> WolframTextResult:
        if not isinstance(expression, str) or not expression.strip() or "\x00" in expression:
            raise WolframWorkerError("invalid_output", "wolfram expression is invalid")
        if len(expression.encode("utf-8")) > MAX_EXPRESSION_BYTES:
            raise WolframWorkerError("invalid_output", "wolfram expression exceeds its byte limit")
        if not 1 <= timeout_seconds <= 3_600:
            raise ValueError("worker timeout must be between 1 and 3600 seconds")
        worker_timeout = max(1, int(timeout_seconds))
        response = await self._call(
            {
                "op": "eval",
                "expr": expression,
                "timeout": worker_timeout,
            },
            timeout_seconds=timeout_seconds + 8.0,
        )
        if response.get("ok") is not True:
            raise WolframWorkerError(
                "execution_failed",
                "wolfram worker rejected or failed the bounded expression",
            )
        if set(response) - {"ok", "kind", "text", "rotating"}:
            raise WolframWorkerError(
                "invalid_output",
                "wolfram worker returned unexpected response fields",
            )
        if "rotating" in response and type(response["rotating"]) is not bool:
            raise WolframWorkerError(
                "invalid_output",
                "wolfram worker returned an invalid rotation marker",
            )
        if response.get("kind") != "text" or not isinstance(response.get("text"), str):
            raise WolframWorkerError(
                "invalid_output",
                "text-only wolfram provider rejected a non-text result",
            )
        text = response["text"]
        if not text or len(text) > 2_000 or len(text.encode("utf-8")) > 16 * 1024:
            raise WolframWorkerError(
                "invalid_output",
                "wolfram worker text result exceeded its bounds",
            )
        return WolframTextResult(text=text)

    async def _call(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        self.validate_socket_authority()
        encoded = canonicalize_json_value(payload).canonical_bytes + b"\n"
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    str(self.socket_path),
                    limit=MAX_RESPONSE_BYTES + 1,
                ),
                timeout=self.connect_timeout_seconds,
            )
            writer.write(encoded)
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
            if not raw or not raw.endswith(b"\n") or len(raw) > MAX_RESPONSE_BYTES:
                raise WolframWorkerError(
                    "invalid_output",
                    "wolfram worker response violated its transport bound",
                )
            response = strict_json_loads(raw[:-1])
            if not isinstance(response, dict):
                raise WolframWorkerError(
                    "invalid_output",
                    "wolfram worker returned a non-object response",
                )
            return response
        except WolframWorkerError:
            raise
        except TimeoutError as exc:
            raise WolframWorkerError(
                "timeout",
                "wolfram worker exceeded its hard wall time",
            ) from exc
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise WolframWorkerError(
                "invalid_output",
                "wolfram worker returned malformed bounded JSON",
            ) from exc
        except OSError as exc:
            raise WolframWorkerError(
                "internal_error",
                "wolfram worker transport failed safely",
            ) from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
