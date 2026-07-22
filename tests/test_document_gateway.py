from hashlib import sha256
from pathlib import Path
import struct
import zlib

import httpx
import pytest

from superlily_contracts import RenderDocument
from superlily_latex_provider.document_gateway import GatewaySettings, create_app
from superlily_latex_provider.runtime import LatexPngResult, LatexWorkerClient


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(scanlines)
    ) + chunk(b"IEND", b"")


@pytest.mark.asyncio
async def test_document_gateway_authenticates_and_returns_strict_png(monkeypatch) -> None:
    body = _png(3, 4)

    async def health(self):
        del self
        return {"status": "ready"}

    async def render_document(self, document, *, timeout_seconds):
        del self, document, timeout_seconds
        return LatexPngResult(body, sha256(body).hexdigest(), 3, 4)

    monkeypatch.setattr(LatexWorkerClient, "health", health)
    monkeypatch.setattr(LatexWorkerClient, "render_document", render_document)
    app = create_app(GatewaySettings(Path("/tmp/worker.sock"), "g" * 32))
    transport = httpx.ASGITransport(app=app)
    document = RenderDocument(
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        blocks=[{"kind": "text", "text": "手机渲染"}],
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/ready")).status_code == 200
        unauthorized = await client.post(
            "/render-document", json=document.model_dump(mode="json")
        )
        assert unauthorized.status_code == 401
        rendered = await client.post(
            "/render-document",
            json=document.model_dump(mode="json"),
            headers={"Authorization": "Bearer " + "g" * 32},
        )
    assert rendered.status_code == 200
    assert rendered.content == body
    assert rendered.headers["content-sha256"] == sha256(body).hexdigest()
    assert rendered.headers["x-width-pixels"] == "3"
    assert rendered.headers["x-height-pixels"] == "4"
