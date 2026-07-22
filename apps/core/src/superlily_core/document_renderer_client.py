"""Backend-neutral authenticated HTTP client for document rendering."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re

import httpx

from superlily_contracts import RenderDocument


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


class DocumentRendererError(RuntimeError):
    def __init__(self, error_code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.error_code = error_code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class RenderedDocument:
    content: bytes
    content_sha256: str
    width_pixels: int
    height_pixels: int


class DocumentRendererClient:
    """Core contract remains stable when the isolated backend implementation changes."""

    def __init__(self, backend_url: str, token: str, *, connect_timeout_seconds: float = 3.0) -> None:
        if not backend_url.startswith("http://") or backend_url.rstrip("/") != backend_url:
            raise ValueError("render backend URL must be an exact internal HTTP origin")
        if len(token) < 32:
            raise ValueError("render backend token must contain at least 32 characters")
        self.backend_url = backend_url
        self.token = token
        self.connect_timeout_seconds = connect_timeout_seconds

    async def render_document(
        self,
        document: RenderDocument,
        *,
        timeout_seconds: float,
    ) -> RenderedDocument:
        timeout = httpx.Timeout(timeout_seconds, connect=self.connect_timeout_seconds)
        try:
            async with httpx.AsyncClient(base_url=self.backend_url, timeout=timeout) as client:
                response = await client.post(
                    "/render-document",
                    json=document.model_dump(mode="json"),
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        except httpx.TimeoutException as exc:
            raise DocumentRendererError("timeout", "document renderer timed out") from exc
        except httpx.HTTPError as exc:
            raise DocumentRendererError("internal_error", "document renderer transport failed") from exc
        if response.status_code == 401:
            raise DocumentRendererError("internal_error", "document renderer authentication failed")
        if response.status_code != 200:
            code = response.headers.get("X-Render-Error-Code", "execution_failed")
            if code not in {"timeout", "execution_failed", "invalid_output", "budget_exceeded", "internal_error"}:
                code = "execution_failed"
            raise DocumentRendererError(code, "document renderer failed safely")
        content = response.content
        content_hash = response.headers.get("Content-SHA256", "")
        try:
            width = int(response.headers["X-Width-Pixels"])
            height = int(response.headers["X-Height-Pixels"])
        except (KeyError, ValueError) as exc:
            raise DocumentRendererError("invalid_output", "renderer metadata is invalid") from exc
        if (
            response.headers.get("content-type", "").split(";", 1)[0] != "image/png"
            or not 1 <= len(content) <= MAX_ARTIFACT_BYTES
            or not content.startswith(_PNG_SIGNATURE)
            or not _SHA256_RE.fullmatch(content_hash)
            or sha256(content).hexdigest() != content_hash
            or not 1 <= width <= 4_096
            or not 1 <= height <= 4_096
        ):
            raise DocumentRendererError("invalid_output", "renderer artifact failed validation")
        return RenderedDocument(content, content_hash, width, height)
