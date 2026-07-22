"""Authenticated network boundary in front of the credential-free renderer worker."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import stat

from fastapi import FastAPI, Header, HTTPException, Response, status
import uvicorn

from superlily_contracts import RenderDocument

from .runtime import LatexWorkerClient, LatexWorkerError


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    worker_socket: Path
    token: str
    render_timeout_seconds: float = 40.0

    @classmethod
    def from_env(cls) -> "GatewaySettings":
        token_file = Path(os.environ["SUPERLILY_DOCUMENT_RENDERER_TOKEN_FILE"])
        if not token_file.is_absolute():
            raise ValueError("document renderer token file must be absolute")
        entry = token_file.lstat()
        if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode) or stat.S_IMODE(entry.st_mode) & 0o022:
            raise ValueError("document renderer token file failed authority checks")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("document renderer token must contain at least 32 characters")
        return cls(
            worker_socket=Path(
                os.getenv("SUPERLILY_DOCUMENT_RENDERER_WORKER_SOCKET", "/latex-ipc/worker.sock")
            ),
            token=token,
            render_timeout_seconds=float(
                os.getenv("SUPERLILY_DOCUMENT_RENDERER_TIMEOUT_SECONDS", "40")
            ),
        )


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        return ""
    return authorization[7:]


def create_app(settings: GatewaySettings) -> FastAPI:
    app = FastAPI(title="Superlily Document Renderer", docs_url=None, redoc_url=None)
    worker = LatexWorkerClient(settings.worker_socket)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            health = await worker.health()
        except LatexWorkerError as exc:
            raise HTTPException(status_code=503, detail="renderer worker unavailable") from exc
        return {"status": "ok", "worker": str(health["status"])}

    @app.post("/render-document")
    async def render_document(
        document: RenderDocument,
        authorization: str | None = Header(default=None),
    ) -> Response:
        if not hmac.compare_digest(_bearer(authorization), settings.token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        try:
            result = await worker.render_document(
                document,
                timeout_seconds=settings.render_timeout_seconds,
            )
        except LatexWorkerError as exc:
            return Response(
                status_code=status.HTTP_502_BAD_GATEWAY,
                headers={"X-Render-Error-Code": exc.error_code},
            )
        return Response(
            content=result.content,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Content-SHA256": result.content_sha256,
                "X-Width-Pixels": str(result.width_pixels),
                "X-Height-Pixels": str(result.height_pixels),
            },
        )

    return app


def main() -> None:
    uvicorn.run(create_app(GatewaySettings.from_env()), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
