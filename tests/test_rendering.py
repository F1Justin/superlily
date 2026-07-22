from __future__ import annotations

from hashlib import sha256
import struct
import zlib

import httpx
import pytest
from pydantic import ValidationError

from superlily_contracts import RenderDocument, render_document_hash, split_inline_math
from superlily_core.app import create_app
from superlily_core.document_renderer_client import DocumentRendererClient, RenderedDocument
from superlily_core.models import BotInstance, RenderDeliveryAttempt
from superlily_core.settings import Settings
from superlily_latex_provider.worker import document_latex


def _png(width: int = 2, height: int = 3) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(body, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(scanlines)
    ) + chunk(b"IEND", b"")


def _document() -> RenderDocument:
    return RenderDocument(
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        title="典范同构练习",
        blocks=[
            {"kind": "text", "text": "中文与数学符号不再交给 Matplotlib 排版。"},
            {"kind": "math", "latex": r"V \cong (V^*)^* \otimes W"},
            {"kind": "list", "ordered": True, "items": ["第一题", "第二题"]},
        ],
    )


def test_render_contract_is_canonical_and_rejects_tex_file_io() -> None:
    document = _document()
    assert render_document_hash(document) == render_document_hash(
        RenderDocument.model_validate(document.model_dump(mode="json"))
    )
    source = document_latex(document)
    assert r"\usepackage[punct=kaiming,fontset=none]{ctex}" in source
    assert r"\setCJKmainfont{Noto Serif CJK SC}" in source
    assert r"\setCJKmathfont{Noto Serif CJK SC}" in source
    assert "中文与数学符号" in source
    assert r"V \cong (V^*)^* \otimes W" in source
    assert r"\begin{enumerate}" in source
    with pytest.raises(ValidationError):
        RenderDocument(
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_1080353942",
            blocks=[{"kind": "math", "latex": r"\input{/etc/passwd}"}],
        )


def test_prose_supports_safe_inline_math_without_block_fragmentation() -> None:
    document = RenderDocument(
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        title=r"倒数根与 $C^{-1}$",
        blocks=[
            {
                "kind": "text",
                "text": r"已知 $f(x)=x^3+px^2+qx+r$，其根为 $\lambda_1,\lambda_2,\lambda_3$。",
            },
            {"kind": "list", "items": [r"计算 $\det(yC-I)$", r"价格为 \$5"]},
        ],
    )

    source = document_latex(document)
    assert r"倒数根与 \(C^{-1}\)" in source
    assert r"已知 \(f(x)=x^3+px^2+qx+r\)，其根为 \(\lambda_1,\lambda_2,\lambda_3\)" in source
    assert r"\item 计算 \(\det(yC-I)\)" in source
    assert r"\item 价格为 \$5" in source
    assert "$f(x)" not in source
    assert split_inline_math(r"金额 \$5，变量 $x$，末尾 $") == (
        ("text", "金额 $5，变量 "),
        ("math", "x"),
        ("text", "，末尾 $"),
    )


@pytest.mark.parametrize("field", ["title", "text", "heading", "list"])
def test_inline_math_rejects_forbidden_tex_commands(field: str) -> None:
    unsafe = r"不要 $\input{/etc/passwd}$"
    kwargs: dict[str, object] = {
        "instance_id": "nekro-agent",
        "conversation_key": "onebot_v11-group_1080353942",
        "blocks": [{"kind": "text", "text": "安全正文"}],
    }
    if field == "title":
        kwargs["title"] = unsafe
    elif field == "text":
        kwargs["blocks"] = [{"kind": "text", "text": unsafe}]
    elif field == "heading":
        kwargs["blocks"] = [{"kind": "heading", "text": unsafe}]
    else:
        kwargs["blocks"] = [{"kind": "list", "items": [unsafe]}]
    with pytest.raises(ValidationError):
        RenderDocument(**kwargs)


@pytest.mark.asyncio
async def test_render_api_enforces_canary_stores_artifact_and_records_delivery(
    tmp_path, monkeypatch
) -> None:
    body = _png()

    async def render_document(self, document, *, timeout_seconds):
        del self, document, timeout_seconds
        return RenderedDocument(
            content=body,
            content_sha256=sha256(body).hexdigest(),
            width_pixels=2,
            height_pixels=3,
        )

    monkeypatch.setattr(DocumentRendererClient, "render_document", render_document)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'core.db'}",
        ingest_tokens={"nekro-agent": "nekro-secret"},
        group_default_mode="full",
        artifact_root=str(tmp_path / "artifacts"),
        artifact_secret_pepper="p" * 32,
        render_mode="canary",
        render_canary_conversations=frozenset({"onebot_v11-group_1080353942"}),
        render_backend_url="http://document-renderer:8000",
        render_backend_token="r" * 32,
    )
    app = create_app(settings)
    await app.state.database.create_schema()
    try:
        async with app.state.database.sessions() as session:
            session.add(
                BotInstance(
                    id="nekro-agent",
                    platform="qq",
                    adapter="onebot_v11",
                    bot_id="2022692714",
                    role="talk",
                )
            )
            await session.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {
                "Authorization": "Bearer nekro-secret",
                "Idempotency-Key": "render-exercise-0001",
            }
            created = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=headers,
            )
            assert created.status_code == 201, created.text
            receipt = created.json()
            assert receipt["content_sha256"] == sha256(body).hexdigest()
            assert receipt["render_duration_ms"] >= 0
            duplicate = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=headers,
            )
            assert duplicate.status_code == 200
            assert duplicate.json()["artifact_id"] == receipt["artifact_id"]
            downloaded = await client.get(
                receipt["content_path"],
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert downloaded.status_code == 200
            assert downloaded.content == body
            delivered = await client.post(
                f"/v1/render-artifacts/{receipt['artifact_id']}/delivery-attempts",
                json={
                    "instance_id": "nekro-agent",
                    "outcome": "ambiguous",
                    "safe_error_code": "platform_message_id_unavailable",
                },
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert delivered.status_code == 201
        async with app.state.database.sessions() as session:
            assert await session.get(RenderDeliveryAttempt, delivered.json()["attempt_id"])
    finally:
        await app.state.database.drop_schema()
        await app.state.database.dispose()
