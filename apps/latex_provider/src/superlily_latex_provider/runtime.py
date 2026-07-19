"""通过私有 Unix socket 调用无网络、无凭据的 LaTeX worker。"""

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


PROVIDER_ID = "provider-latex-primary"
TOOL_ID = "latex.render"
DESCRIPTOR_VERSION = "1.0.0"
MIME_TYPE = "image/png"
MAX_LATEX_BYTES = 8 * 1024
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_DIMENSION_PIXELS = 2_048
MAX_HEADER_BYTES = 4 * 1024
MAX_REQUEST_BYTES = 32 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[\x20-\x7e]{1,128}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class LatexWorkerError(RuntimeError):
    """不包含公式、编译日志或本地路径的有界失败。"""

    def __init__(
        self,
        error_code: Literal[
            "timeout",
            "execution_failed",
            "invalid_output",
            "budget_exceeded",
            "internal_error",
        ],
        safe_detail: str,
    ) -> None:
        super().__init__(safe_detail)
        self.error_code = error_code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class LatexPngResult:
    content: bytes
    content_sha256: str
    width_pixels: int
    height_pixels: int


def inspect_png(content: bytes) -> tuple[int, int]:
    """只接受带标准 IHDR 的有界 PNG；Core 上传时还会再次独立检查。"""

    if len(content) < 33 or not content.startswith(_PNG_SIGNATURE):
        raise LatexWorkerError("invalid_output", "latex worker did not return a PNG artifact")
    if int.from_bytes(content[8:12], "big") != 13 or content[12:16] != b"IHDR":
        raise LatexWorkerError("invalid_output", "latex worker returned a malformed PNG header")
    width = int.from_bytes(content[16:20], "big")
    height = int.from_bytes(content[20:24], "big")
    if not 1 <= width <= MAX_DIMENSION_PIXELS or not 1 <= height <= MAX_DIMENSION_PIXELS:
        raise LatexWorkerError("budget_exceeded", "latex PNG dimensions exceeded the hard bound")
    return width, height


def build_worker_identity_hash(
    *,
    image_id: str,
    worker_sha256: str,
    template_sha256: str,
    tex_version: str,
    poppler_version: str,
    sandbox_profile_sha256: str,
) -> str:
    """绑定 worker 镜像、源码、模板、引擎和部署隔离配置。"""

    if not _IMAGE_ID_RE.fullmatch(image_id):
        raise ValueError("worker image ID must be an exact Docker SHA-256")
    if not all(
        _SHA256_RE.fullmatch(value)
        for value in (worker_sha256, template_sha256, sandbox_profile_sha256)
    ):
        raise ValueError("worker source, template and sandbox hashes must be lowercase SHA-256")
    if not _VERSION_RE.fullmatch(tex_version) or not _VERSION_RE.fullmatch(poppler_version):
        raise ValueError("renderer versions must be exact bounded visible text")
    return canonicalize_json_value(
        {
            "image_id": image_id,
            "poppler_version": poppler_version,
            "sandbox_profile_sha256": sandbox_profile_sha256,
            "schema_version": "1.0",
            "template_sha256": template_sha256,
            "tex_version": tex_version,
            "transport": "superlily-latex-unix-framed-v1",
            "worker_sha256": worker_sha256,
        }
    ).sha256


def latex_implementation_hash(worker_identity_hash: str) -> str:
    """把 Provider 源码和显式部署的 worker 后端身份绑定为实现身份。"""

    if not _SHA256_RE.fullmatch(worker_identity_hash):
        raise ValueError("worker_identity_hash must be a lowercase SHA-256")
    digest = sha256()
    for name in ("main.py", "runtime.py"):
        source = Path(__file__).with_name(name).read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name.encode("utf-8"))
        digest.update(len(source).to_bytes(8, "big"))
        digest.update(source)
    label = b"latex-worker-identity-v1"
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(bytes.fromhex(worker_identity_hash))
    return digest.hexdigest()


class LatexWorkerClient:
    """一次请求一个连接；响应头与 PNG 正文都有硬字节边界且不自动重试。"""

    def __init__(self, socket_path: Path, *, connect_timeout_seconds: float = 3.0) -> None:
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
            raise LatexWorkerError("internal_error", "latex worker socket is unavailable") from exc
        uid = os.getuid()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != uid
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise LatexWorkerError(
                "internal_error", "latex worker socket directory failed authority checks"
            )
        if (
            not stat.S_ISSOCK(endpoint.st_mode)
            or stat.S_ISLNK(endpoint.st_mode)
            or endpoint.st_uid != uid
            or stat.S_IMODE(endpoint.st_mode) != 0o600
        ):
            raise LatexWorkerError("internal_error", "latex worker socket failed authority checks")

    async def health(self) -> dict[str, Any]:
        header, content = await self._call({"op": "health"}, timeout_seconds=5.0)
        if content:
            raise LatexWorkerError("invalid_output", "latex worker health response had a body")
        if (
            set(header)
            != {"ok", "status", "requests", "uid", "pid", "tex_version", "poppler_version"}
            or header.get("ok") is not True
            or header.get("status") != "ready"
            or type(header.get("requests")) is not int
            or header["requests"] < 0
            or header.get("uid") != os.getuid()
            or type(header.get("pid")) is not int
            or header["pid"] <= 0
            or not isinstance(header.get("tex_version"), str)
            or not _VERSION_RE.fullmatch(header["tex_version"])
            or not isinstance(header.get("poppler_version"), str)
            or not _VERSION_RE.fullmatch(header["poppler_version"])
        ):
            raise LatexWorkerError("internal_error", "latex worker returned invalid health metadata")
        return {
            "status": "ready",
            "requests": header["requests"],
            "uid": header["uid"],
            "tex_version": header["tex_version"],
            "poppler_version": header["poppler_version"],
        }

    async def render(self, latex: str, *, timeout_seconds: float) -> LatexPngResult:
        if not isinstance(latex, str) or not latex.strip() or "\x00" in latex:
            raise LatexWorkerError("invalid_output", "latex input is invalid")
        if len(latex.encode("utf-8")) > MAX_LATEX_BYTES:
            raise LatexWorkerError("budget_exceeded", "latex input exceeded its byte limit")
        if not 1 <= timeout_seconds <= 3_600:
            raise ValueError("worker timeout must be between 1 and 3600 seconds")
        header, content = await self._call(
            {"op": "render", "latex": latex},
            timeout_seconds=timeout_seconds,
        )
        if header.get("ok") is not True:
            allowed_codes = {
                "timeout",
                "execution_failed",
                "invalid_output",
                "budget_exceeded",
                "internal_error",
            }
            code = header.get("error_code")
            if code not in allowed_codes:
                code = "execution_failed"
            raise LatexWorkerError(code, "latex worker failed the bounded render")  # type: ignore[arg-type]
        if set(header) != {
            "ok",
            "mime_type",
            "byte_size",
            "content_sha256",
            "width_pixels",
            "height_pixels",
        }:
            raise LatexWorkerError("invalid_output", "latex worker returned unexpected metadata")
        if (
            header.get("mime_type") != MIME_TYPE
            or type(header.get("byte_size")) is not int
            or header["byte_size"] != len(content)
            or not 1 <= len(content) <= MAX_ARTIFACT_BYTES
            or not isinstance(header.get("content_sha256"), str)
            or not _SHA256_RE.fullmatch(header["content_sha256"])
            or sha256(content).hexdigest() != header["content_sha256"]
        ):
            raise LatexWorkerError("invalid_output", "latex worker artifact metadata did not match")
        width, height = inspect_png(content)
        if (header.get("width_pixels"), header.get("height_pixels")) != (width, height):
            raise LatexWorkerError("invalid_output", "latex worker PNG dimensions did not match")
        return LatexPngResult(
            content=content,
            content_sha256=header["content_sha256"],
            width_pixels=width,
            height_pixels=height,
        )

    async def _call(
        self, payload: dict[str, Any], *, timeout_seconds: float
    ) -> tuple[dict[str, Any], bytes]:
        self.validate_socket_authority()
        encoded = canonicalize_json_value(payload).canonical_bytes + b"\n"
        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path), limit=MAX_HEADER_BYTES + 1),
                timeout=self.connect_timeout_seconds,
            )
            writer.write(encoded)
            await writer.drain()
            raw_header = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
            if (
                not raw_header
                or not raw_header.endswith(b"\n")
                or len(raw_header) > MAX_HEADER_BYTES
            ):
                raise LatexWorkerError("invalid_output", "latex worker response header was invalid")
            header = strict_json_loads(raw_header[:-1])
            if not isinstance(header, dict):
                raise LatexWorkerError("invalid_output", "latex worker returned a non-object header")
            byte_size = header.get("byte_size", 0) if header.get("ok") is True else 0
            if type(byte_size) is not int or not 0 <= byte_size <= MAX_ARTIFACT_BYTES:
                raise LatexWorkerError("budget_exceeded", "latex worker response exceeded its bound")
            content = (
                await asyncio.wait_for(reader.readexactly(byte_size), timeout=timeout_seconds)
                if byte_size
                else b""
            )
            return header, content
        except LatexWorkerError:
            raise
        except TimeoutError as exc:
            raise LatexWorkerError("timeout", "latex worker exceeded its hard wall time") from exc
        except (asyncio.IncompleteReadError, ValueError) as exc:
            raise LatexWorkerError("invalid_output", "latex worker returned malformed bounded data") from exc
        except OSError as exc:
            raise LatexWorkerError("internal_error", "latex worker transport failed safely") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
