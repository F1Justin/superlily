from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import os
from pathlib import Path
from typing import Any

import pytest

from superlily_contracts import (
    ToolArtifactReference,
    ToolArtifactReservationOut,
    ToolArtifactUploadOut,
    ToolLeaseOut,
    ToolUsage,
    canonicalize_json_value,
    load_tool_descriptor,
)
from superlily_latex_provider.main import (
    LatexExecutor,
    LatexProviderConfig,
    _load_runtime,
    _store_artifact,
)
from superlily_latex_provider.runtime import (
    LatexPngResult,
    LatexWorkerClient,
    LatexWorkerError,
    build_worker_identity_hash,
    latex_implementation_hash,
)
from superlily_latex_provider.worker import render_latex_png, template_sha256

from test_artifact_store import png_bytes


DESCRIPTOR_PATH = Path("registry/descriptors/latex.render/1.0.0.json")
WORKER_IDENTITY = "a" * 64


async def _worker_server(
    directory: Path,
    header: dict[str, Any],
    body: bytes = b"",
) -> tuple[asyncio.AbstractServer, Path]:
    directory.chmod(0o700)
    socket_path = directory / "worker.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            writer.write(canonicalize_json_value(header).canonical_bytes + b"\n" + body)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = await asyncio.start_unix_server(handle, path=socket_path)
    socket_path.chmod(0o600)
    return server, socket_path


def _executor(socket_path: Path) -> LatexExecutor:
    return LatexExecutor(
        DESCRIPTOR_PATH.read_bytes(),
        worker_identity_hash=WORKER_IDENTITY,
        worker_socket=socket_path,
        connect_timeout_seconds=1,
    )


def _config(socket_path: Path) -> LatexProviderConfig:
    return LatexProviderConfig(
        core_url="http://core.test",
        token="provider-token",
        worker_identity_hash=WORKER_IDENTITY,
        descriptor_path=DESCRIPTOR_PATH,
        worker_socket=socket_path,
        heartbeat_seconds=5,
        inventory_seconds=5,
        http_timeout_seconds=1,
        connect_timeout_seconds=1,
        poll_seconds=0.05,
        max_idle_poll_seconds=0.1,
        execution_heartbeat_seconds=0.1,
    )


def test_latex_provider_production_idle_poll_is_bounded_for_fast_pickup() -> None:
    config = LatexProviderConfig(
        core_url="http://core.test",
        token="provider-token",
        worker_identity_hash=WORKER_IDENTITY,
    )
    assert config.poll_seconds == 0.25
    assert config.max_idle_poll_seconds == 1


def test_latex_descriptor_binds_one_png_and_all_hard_artifact_bounds() -> None:
    loaded = load_tool_descriptor(DESCRIPTOR_PATH.read_bytes())
    descriptor = loaded.descriptor
    assert descriptor.tool_id == "latex.render"
    assert descriptor.version == "1.0.0"
    assert descriptor.natural_language is False
    assert descriptor.allowed_callers == ["command", "admin_api"]
    assert descriptor.execution_permissions.network == "deny"
    assert descriptor.execution_permissions.filesystem == "sandbox_only"
    assert descriptor.execution_permissions.subprocess == "sandbox_only"
    assert descriptor.execution_permissions.artifacts == ["image/png"]
    assert descriptor.resource_budget.artifact_bytes == 4 * 1024 * 1024
    assert descriptor.artifact_policy is not None
    assert descriptor.artifact_policy.max_count == 1
    assert descriptor.artifact_policy.max_width_pixels == 2048
    assert descriptor.artifact_policy.max_height_pixels == 2048
    assert "artifact_bytes" in descriptor.required_budget_enforcement


def test_implementation_hash_binds_worker_image_template_engine_and_sandbox() -> None:
    identity = build_worker_identity_hash(
        image_id="sha256:" + "0" * 64,
        worker_sha256="1" * 64,
        template_sha256=template_sha256(),
        tex_version="XeTeX 2024",
        poppler_version="pdftoppm 25.03.0",
        sandbox_profile_sha256="3" * 64,
    )
    assert len(identity) == 64
    assert identity == build_worker_identity_hash(
        image_id="sha256:" + "0" * 64,
        worker_sha256="1" * 64,
        template_sha256=template_sha256(),
        tex_version="XeTeX 2024",
        poppler_version="pdftoppm 25.03.0",
        sandbox_profile_sha256="3" * 64,
    )
    assert latex_implementation_hash("1" * 64) != latex_implementation_hash("2" * 64)
    with pytest.raises(ValueError, match="image ID"):
        build_worker_identity_hash(
            image_id="latest",
            worker_sha256="1" * 64,
            template_sha256=template_sha256(),
            tex_version="XeTeX 2024",
            poppler_version="pdftoppm 25.03.0",
            sandbox_profile_sha256="3" * 64,
        )


def test_reporter_is_ineligible_while_executor_reports_all_hard_bounds(tmp_path: Path) -> None:
    report_executor, reported = _load_runtime(_config(tmp_path / "worker.sock"), execution_enabled=False)
    run_executor, executable = _load_runtime(_config(tmp_path / "worker.sock"), execution_enabled=True)
    assert report_executor.implementation_hash == run_executor.implementation_hash
    assert set(reported.inventory_entry.budget_enforcement.values()) == {"unsupported"}
    assert executable.inventory_entry.budget_enforcement == {
        "artifact_bytes": "hard",
        "input_bytes": "hard",
        "memory": "hard",
        "output_bytes": "hard",
        "wall_time": "hard",
    }


@pytest.mark.asyncio
async def test_worker_health_and_png_response_are_strict_and_content_addressed(tmp_path: Path) -> None:
    server, socket_path = await _worker_server(
        tmp_path,
        {
            "ok": True,
            "status": "ready",
            "requests": 2,
            "uid": os.getuid(),
            "pid": 42,
            "tex_version": "XeTeX 2024",
            "poppler_version": "pdftoppm 25.03.0",
        },
    )
    try:
        health = await LatexWorkerClient(socket_path).health()
        assert health["status"] == "ready"
        assert health["requests"] == 2
    finally:
        server.close()
        await server.wait_closed()

    socket_path.unlink(missing_ok=True)
    body = png_bytes(17, 9)
    server, socket_path = await _worker_server(
        tmp_path,
        {
            "ok": True,
            "mime_type": "image/png",
            "byte_size": len(body),
            "content_sha256": sha256(body).hexdigest(),
            "width_pixels": 17,
            "height_pixels": 9,
        },
        body,
    )
    try:
        result = await _executor(socket_path).execute({"latex": "x^2"}, timeout_seconds=2)
        assert result.outcome == "success"
        assert result.artifact is not None
        assert result.artifact.content == body
        assert (result.artifact.width_pixels, result.artifact.height_pixels) == (17, 9)
        assert result.usage.artifact_bytes == len(body)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_worker_hash_mismatch_and_raw_error_fields_fail_closed(tmp_path: Path) -> None:
    body = png_bytes()
    server, socket_path = await _worker_server(
        tmp_path,
        {
            "ok": True,
            "mime_type": "image/png",
            "byte_size": len(body),
            "content_sha256": "0" * 64,
            "width_pixels": 1,
            "height_pixels": 1,
        },
        body,
    )
    try:
        with pytest.raises(LatexWorkerError, match="metadata"):
            await LatexWorkerClient(socket_path).render("secret-formula", timeout_seconds=2)
    finally:
        server.close()
        await server.wait_closed()

    socket_path.unlink(missing_ok=True)
    server, socket_path = await _worker_server(
        tmp_path,
        {"ok": False, "error_code": "execution_failed", "raw_log": "secret-formula"},
    )
    try:
        with pytest.raises(LatexWorkerError) as failure:
            await LatexWorkerClient(socket_path).render("secret-formula", timeout_seconds=2)
        assert "secret-formula" not in failure.value.safe_detail
    finally:
        server.close()
        await server.wait_closed()


def test_real_renderer_produces_bounded_png_and_never_exposes_bad_formula(tmp_path: Path) -> None:
    xelatex = Path("/usr/local/texlive/2024/bin/x86_64-linux/xelatex")
    if not xelatex.is_file():
        pytest.skip("host TeX Live is unavailable")
    body = render_latex_png("x^2+y^2=z^2", work_root=tmp_path)
    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert 1 <= len(body) <= 4 * 1024 * 1024
    secret_formula = r"\def\secretmarker{do-not-leak}\undefinedcommand"
    with pytest.raises(LatexWorkerError) as failure:
        render_latex_png(secret_formula, work_root=tmp_path)
    assert "do-not-leak" not in failure.value.safe_detail


def test_real_renderer_denies_absolute_input_and_shell_escape(tmp_path: Path) -> None:
    xelatex = Path("/usr/local/texlive/2024/bin/x86_64-linux/xelatex")
    if not xelatex.is_file():
        pytest.skip("host TeX Live is unavailable")
    with pytest.raises(LatexWorkerError, match="failed safely"):
        render_latex_png(r"\input{/etc/passwd}", work_root=tmp_path)
    marker = tmp_path / "shell-escape-marker"
    body = render_latex_png(
        rf"\immediate\write18{{touch {marker}}}x",
        work_root=tmp_path,
    )
    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert not marker.exists()


class _ArtifactClient:
    def __init__(self, body: bytes, width: int, height: int) -> None:
        self.body = body
        self.width = width
        self.height = height
        self.calls: list[str] = []

    async def heartbeat(self, *_: object, **__: object) -> dict[str, Any]:
        self.calls.append("heartbeat")
        return {"cancel_requested": False}

    async def reserve_artifact(self, invocation_id: str, payload: Any, *, idempotency_key: str):
        self.calls.append("reserve")
        assert payload.declared_sha256 == sha256(self.body).hexdigest()
        assert idempotency_key.startswith("latex-artifact:")
        return ToolArtifactReservationOut(
            artifact_id="artifact-1",
            invocation_id=invocation_id,
            attempt_id=payload.attempt_id,
            fencing_token=payload.fencing_token,
            upload_secret="u" * 43,
            mime_type="image/png",
            max_bytes=4 * 1024 * 1024,
            max_width_pixels=2048,
            max_height_pixels=2048,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=120),
        )

    async def upload_artifact(self, artifact_id: str, *, content: bytes, **_: object):
        self.calls.append("upload")
        assert artifact_id == "artifact-1"
        assert content == self.body
        return ToolArtifactUploadOut(
            artifact_id=artifact_id,
            state="uploading",
            content_sha256=sha256(content).hexdigest(),
            mime_type="image/png",
            byte_size=len(content),
            width_pixels=self.width,
            height_pixels=self.height,
        )

    async def finalize_artifact(self, invocation_id: str, payload: Any):
        self.calls.append("finalize")
        assert invocation_id == "invocation-1"
        return ToolArtifactReference.model_validate(payload.model_dump(exclude={"schema_version", "attempt_id", "fencing_token", "lease_secret"}))


@pytest.mark.asyncio
async def test_provider_uses_reserve_upload_finalize_before_building_output(tmp_path: Path) -> None:
    body = png_bytes(12, 7)
    artifact = LatexPngResult(
        content=body,
        content_sha256=sha256(body).hexdigest(),
        width_pixels=12,
        height_pixels=7,
    )
    client = _ArtifactClient(body, 12, 7)
    executor = _executor(tmp_path / "unused.sock")
    lease = ToolLeaseOut.model_validate(
        {
            "invocation_id": "invocation-1",
            "attempt_id": "attempt-1",
            "attempt_number": 1,
            "fencing_token": 1,
            "lease_secret": "s" * 43,
            "provider_id": "provider-latex-primary",
            "inventory_hash": "1" * 64,
            "implementation_hash": "2" * 64,
            "tool_id": "latex.render",
            "descriptor_version": "1.0.0",
            "descriptor_hash": load_tool_descriptor(DESCRIPTOR_PATH.read_bytes()).authority.sha256,
            "input": {"latex": "x^2"},
            "input_hash": canonicalize_json_value({"latex": "x^2"}).sha256,
            "resource_budget": load_tool_descriptor(DESCRIPTOR_PATH.read_bytes()).descriptor.resource_budget,
            "execution_permissions": load_tool_descriptor(DESCRIPTOR_PATH.read_bytes()).descriptor.execution_permissions,
            "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=15),
            "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=30),
        }
    )
    reference, output = await _store_artifact(
        client,  # type: ignore[arg-type]
        executor,
        lease,
        artifact,
        ToolUsage(input_bytes=16, artifact_bytes=len(body)),
        heartbeat_seconds=10,
    )
    assert client.calls == ["reserve", "upload", "finalize"]
    assert reference.artifact_id == "artifact-1"
    assert output == {
        "kind": "image",
        "artifact_id": "artifact-1",
        "mime_type": "image/png",
        "content_sha256": sha256(body).hexdigest(),
        "byte_size": len(body),
        "width_pixels": 12,
        "height_pixels": 7,
    }
