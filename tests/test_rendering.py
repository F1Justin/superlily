from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import struct
import zlib

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

from superlily_contracts import (
    RenderDocument,
    inline_content_plain_text,
    render_document_hash,
    render_document_plain_text,
    split_inline_content,
    split_inline_math,
)
from superlily_core.app import create_app
from superlily_core.document_renderer_client import (
    DocumentRendererClient,
    DocumentRendererError,
    RenderedDocument,
)
from superlily_core.models import (
    BotInstance,
    RenderArtifactRecord,
    RenderAttemptRecord,
    RenderDeliveryAttempt,
    RenderDeliveryIntent,
    RenderDocumentRecord,
    utc_now,
)
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
            {
                "kind": "text",
                "node_id": "intro",
                "text": "中文与数学符号不再交给 Matplotlib 排版。",
            },
            {
                "kind": "math",
                "node_id": "main-equation",
                "latex": r"V \cong (V^*)^* \otimes W",
            },
            {
                "kind": "list",
                "node_id": "exercises",
                "ordered": True,
                "items": ["第一题", "第二题"],
            },
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
                "node_id": "prose",
                "text": r"已知 $f(x)=x^3+px^2+qx+r$，其根为 $\lambda_1,\lambda_2,\lambda_3$。",
            },
            {
                "kind": "list",
                "node_id": "tasks",
                "items": [r"计算 $\det(yC-I)$", r"价格为 \$5"],
            },
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


def test_render_document_v12_supports_bounded_markdown_strong() -> None:
    value = r"普通 **抄本之王：$x^2$**，金额 \$5，**未闭合"
    assert split_inline_content(value, markdown_lite=True) == (
        ("text", "普通 "),
        ("strong", r"抄本之王：$x^2$"),
        ("text", "，金额 $5，**未闭合"),
    )
    assert inline_content_plain_text(value, markdown_lite=True) == (
        "普通 抄本之王：$x^2$，金额 $5，**未闭合"
    )

    document = RenderDocument(
        schema_version="1.2",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_861651713",
        title="**传抄过程**",
        blocks=[
            {
                "kind": "list",
                "node_id": "copies",
                "items": [
                    r"**抄本之王：** 现存手稿是 $Codex\ Clarkianus$。",
                    "**修道院传承：** 由僧侣代代传抄。",
                ],
            },
            {
                "kind": "code",
                "node_id": "literal-code",
                "code": "print('**保持原样**')",
            },
        ],
    )

    source = document_latex(document)
    assert r"\textbf{传抄过程}" in source
    assert r"\item \textbf{抄本之王：} 现存手稿是 \(Codex\ Clarkianus\)" in source
    assert r"\item \textbf{修道院传承：} 由僧侣代代传抄" in source
    assert "**抄本之王" not in source
    assert "print('**保持原样**')" in source
    plain = render_document_plain_text(document)
    assert "**抄本之王" not in plain
    assert "- 抄本之王： 现存手稿是 $Codex\\ Clarkianus$。" in plain
    assert "print('**保持原样**')" in plain


def test_render_document_v11_keeps_markdown_markers_literal() -> None:
    document = RenderDocument(
        schema_version="1.1",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_861651713",
        blocks=[
            {
                "kind": "list",
                "node_id": "legacy-list",
                "items": ["**旧语义**保持原样"],
            }
        ],
    )

    assert r"\item **旧语义**保持原样" in document_latex(document)
    assert "- **旧语义**保持原样" in render_document_plain_text(document)


def test_markdown_strong_does_not_bypass_inline_math_validation() -> None:
    with pytest.raises(ValidationError, match="forbidden"):
        RenderDocument(
            schema_version="1.2",
            instance_id="nekro-agent",
            conversation_key="onebot_v11-group_861651713",
            blocks=[
                {
                    "kind": "text",
                    "node_id": "unsafe",
                    "text": r"**危险 $\input{/etc/passwd}$**",
                }
            ],
        )


@pytest.mark.parametrize("field", ["title", "text", "heading", "list"])
def test_inline_math_rejects_forbidden_tex_commands(field: str) -> None:
    unsafe = r"不要 $\input{/etc/passwd}$"
    kwargs: dict[str, object] = {
        "schema_version": "1.0",
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


def test_render_document_v11_supports_bounded_structural_nodes() -> None:
    document = RenderDocument(
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        title="结构化讲解",
        blocks=[
            {
                "kind": "group",
                "node_id": "group",
                "label": "第一部分",
                "blocks": [
                    {
                        "kind": "quote",
                        "node_id": "quote",
                        "text": "定义中的 $V$ 很重要。",
                        "attribution": "教材",
                    },
                    {
                        "kind": "code",
                        "node_id": "code",
                        "language": "python",
                        "code": "answer = 42",
                    },
                    {
                        "kind": "table",
                        "node_id": "table",
                        "columns": ["对象", "维数"],
                        "rows": [["$V$", "$n$"]],
                    },
                    {
                        "kind": "notice",
                        "node_id": "notice",
                        "severity": "warning",
                        "title": "注意",
                        "text": "不要混淆 $V^*$。",
                    },
                    {
                        "kind": "progress",
                        "node_id": "progress",
                        "label": "完成度",
                        "value": 75,
                        "detail": "还差一问",
                    },
                ],
            },
            {
                "kind": "alternative",
                "node_id": "alternative",
                "preferred_option_id": "compact",
                "options": [
                    {
                        "option_id": "compact",
                        "label": "紧凑版",
                        "requires": ["send_image"],
                        "blocks": [
                            {
                                "kind": "text",
                                "node_id": "compact-text",
                                "text": "手机屏幕优先。",
                            }
                        ],
                    },
                    {
                        "option_id": "plain",
                        "label": "文本版",
                        "requires": ["send_text"],
                        "blocks": [
                            {
                                "kind": "text",
                                "node_id": "plain-text",
                                "text": "普通文本。",
                            }
                        ],
                    },
                ],
            },
            {
                "kind": "image",
                "node_id": "image",
                "artifact_id": "core:diagram-1",
                "caption": "交换图",
                "accessibility_text": "一个交换图",
            },
            {
                "kind": "artifact_ref",
                "node_id": "artifact",
                "artifact_id": "core:proof-1",
                "mime_type": "application/pdf",
                "label": "完整证明",
                "accessibility_text": "完整证明的 PDF 制品",
            },
        ],
    )

    source = document_latex(document)
    assert r"\begin{quote}" in source
    assert r"\begin{tabularx}" in source
    assert r"\fcolorbox{orange!70!black}" in source
    assert "手机屏幕优先" in source
    assert "普通文本" not in source
    assert "完整证明的 PDF 制品" in render_document_plain_text(document)

    raw = document.model_dump(mode="json")
    raw["blocks"][1]["node_id"] = "group"
    with pytest.raises(ValidationError, match="unique node_id"):
        RenderDocument.model_validate(raw)

    raw = document.model_dump(mode="json")
    raw["blocks"][2]["accessibility_text"] = None
    with pytest.raises(ValidationError, match="accessibility_text"):
        RenderDocument.model_validate(raw)


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
        render_implementation_hash="1" * 64,
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
                    metadata_json={"capabilities": {"profile": "onebot_v11.qq.v1", "supported": ["send_image", "send_text"], "limits": {}}},
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
            assert receipt["attempt_id"]
            assert receipt["delivery_plan"]["selected_family"] == "image"
            assert receipt["delivery_plan"]["degradation_reasons"] == []
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
            intent = await client.post(
                f"/v1/render-artifacts/{receipt['artifact_id']}/delivery-intents",
                json={
                    "instance_id": "nekro-agent",
                    "delivery_plan_id": receipt["delivery_plan_id"],
                    "idempotency_key": "delivery-exercise-0001",
                },
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert intent.status_code == 201
            assert intent.json()["should_send"] is True
            duplicate_intent = await client.post(
                f"/v1/render-artifacts/{receipt['artifact_id']}/delivery-intents",
                json={
                    "instance_id": "nekro-agent",
                    "delivery_plan_id": receipt["delivery_plan_id"],
                    "idempotency_key": "delivery-exercise-0001",
                },
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert duplicate_intent.status_code == 200
            assert duplicate_intent.json()["should_send"] is False
            delivered = await client.post(
                f"/v1/render-delivery-intents/{intent.json()['intent_id']}/complete",
                json={
                    "instance_id": "nekro-agent",
                    "outcome": "succeeded",
                    "platform_message_id": "qq-message-42",
                },
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert delivered.status_code == 200
            assert delivered.json()["duplicate"] is False
            delivered_again = await client.post(
                f"/v1/render-delivery-intents/{intent.json()['intent_id']}/complete",
                json={
                    "instance_id": "nekro-agent",
                    "outcome": "succeeded",
                    "platform_message_id": "qq-message-42",
                },
                headers={"Authorization": "Bearer nekro-secret"},
            )
            assert delivered_again.status_code == 200
            assert delivered_again.json()["duplicate"] is True
        async with app.state.database.sessions() as session:
            assert await session.get(RenderDeliveryAttempt, delivered.json()["attempt_id"])
            saved_intent = await session.get(RenderDeliveryIntent, intent.json()["intent_id"])
            assert saved_intent is not None
            assert saved_intent.platform_message_id == "qq-message-42"
    finally:
        await app.state.database.drop_schema()
        await app.state.database.dispose()


@pytest.mark.asyncio
async def test_expired_artifact_and_failed_attempt_are_retryable(tmp_path, monkeypatch) -> None:
    body = _png()
    calls = 0
    fail_next = False

    async def render_document(self, document, *, timeout_seconds):
        nonlocal calls, fail_next
        del self, document, timeout_seconds
        calls += 1
        if fail_next:
            fail_next = False
            raise DocumentRendererError("execution_failed", "safe failure")
        return RenderedDocument(
            content=body,
            content_sha256=sha256(body).hexdigest(),
            width_pixels=2,
            height_pixels=3,
        )

    monkeypatch.setattr(DocumentRendererClient, "render_document", render_document)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}",
        ingest_tokens={"nekro-agent": "nekro-secret"},
        group_default_mode="full",
        artifact_root=str(tmp_path / "artifacts"),
        artifact_secret_pepper="p" * 32,
        render_mode="canary",
        render_canary_conversations=frozenset({"onebot_v11-group_1080353942"}),
        render_backend_url="http://document-renderer:8000",
        render_backend_token="r" * 32,
        render_implementation_hash="2" * 64,
    )
    app = create_app(settings)
    await app.state.database.create_schema()
    headers = {
        "Authorization": "Bearer nekro-secret",
        "Idempotency-Key": "retryable-render-0001",
    }
    try:
        async with app.state.database.sessions() as session:
            session.add(
                BotInstance(
                    id="nekro-agent",
                    platform="qq",
                    adapter="onebot_v11",
                    bot_id="2022692714",
                    role="talk",
                    metadata_json={
                        "capabilities": {
                            "profile": "onebot_v11.qq.v1",
                            "supported": ["send_image", "send_text"],
                            "limits": {},
                        }
                    },
                )
            )
            await session.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=headers,
            )
            assert first.status_code == 201, first.text
            first_receipt = first.json()
            async with app.state.database.sessions() as session:
                artifact = await session.get(RenderArtifactRecord, first_receipt["artifact_id"])
                assert artifact is not None
                artifact.created_at = utc_now() - timedelta(hours=2)
                artifact.expires_at = utc_now() - timedelta(hours=1)
                await session.commit()

            rerendered = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=headers,
            )
            assert rerendered.status_code == 201, rerendered.text
            assert rerendered.json()["render_id"] == first_receipt["render_id"]
            assert rerendered.json()["attempt_id"] != first_receipt["attempt_id"]
            assert rerendered.json()["artifact_id"] != first_receipt["artifact_id"]

            fail_next = True
            failed_headers = {**headers, "Idempotency-Key": "retryable-render-0002"}
            failed = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=failed_headers,
            )
            assert failed.status_code == 502
            recovered = await client.post(
                "/v1/render-documents",
                json=_document().model_dump(mode="json"),
                headers=failed_headers,
            )
            assert recovered.status_code == 201, recovered.text
            assert recovered.json()["artifact_id"]
            assert calls == 4

        async with app.state.database.sessions() as session:
            attempts = (
                await session.scalars(
                    select(RenderAttemptRecord).where(
                        RenderAttemptRecord.render_id == recovered.json()["render_id"]
                    )
                )
            ).all()
            assert [attempt.state for attempt in attempts] == ["failed", "succeeded"]
    finally:
        await app.state.database.drop_schema()
        await app.state.database.dispose()


@pytest.mark.asyncio
async def test_stale_running_attempt_is_abandoned_and_text_capability_degrades(
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
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}",
        ingest_tokens={"nekro-agent": "nekro-secret"},
        group_default_mode="full",
        artifact_root=str(tmp_path / "artifacts"),
        artifact_secret_pepper="p" * 32,
        render_mode="canary",
        render_canary_conversations=frozenset({"onebot_v11-group_1080353942"}),
        render_backend_url="http://document-renderer:8000",
        render_backend_token="r" * 32,
        render_implementation_hash="3" * 64,
    )
    app = create_app(settings)
    await app.state.database.create_schema()
    document = _document()
    try:
        async with app.state.database.sessions() as session:
            session.add(
                BotInstance(
                    id="nekro-agent",
                    platform="qq",
                    adapter="onebot_v11",
                    bot_id="2022692714",
                    role="talk",
                    metadata_json={
                        "capabilities": {
                            "profile": "onebot_v11.qq.v1",
                            "supported": ["send_text"],
                            "limits": {},
                        }
                    },
                )
            )
            record = RenderDocumentRecord(
                instance_id="nekro-agent",
                conversation_key=document.conversation_key,
                idempotency_key="stale-render-0001",
                request_sha256=render_document_hash(document),
                document_json=document.model_dump(mode="json"),
                status="pending",
            )
            session.add(record)
            await session.flush()
            stale = RenderAttemptRecord(
                render_id=record.id,
                attempt_number=1,
                fencing_token=1,
                state="running",
                renderer_profile="xelatex-document-v1",
                renderer_snapshot_json={"profile": "stale"},
                renderer_snapshot_hash="4" * 64,
                lease_expires_at=utc_now() - timedelta(seconds=1),
                started_at=utc_now() - timedelta(minutes=1),
            )
            session.add(stale)
            await session.commit()
            stale_id = stale.id

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            recovered = await client.post(
                "/v1/render-documents",
                json=document.model_dump(mode="json"),
                headers={
                    "Authorization": "Bearer nekro-secret",
                    "Idempotency-Key": "stale-render-0001",
                },
            )
            assert recovered.status_code == 201, recovered.text
            plan = recovered.json()["delivery_plan"]
            assert plan["selected_family"] == "text"
            assert plan["fallback_text"].startswith("典范同构练习")
            assert plan["degradation_reasons"] == ["image_unsupported_fallback_to_text"]

        async with app.state.database.sessions() as session:
            stale = await session.get(RenderAttemptRecord, stale_id)
            assert stale is not None
            assert stale.state == "abandoned"
            assert stale.safe_error_code == "render_lease_expired"
    finally:
        await app.state.database.drop_schema()
        await app.state.database.dispose()
