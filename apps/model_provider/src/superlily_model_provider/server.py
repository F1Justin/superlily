"""Resident authenticated DeepSeek trigger service.

Core sends only a run/loop identifier. The provider pulls the frozen planner
input through its existing provider identity and reports the signed attempt
back through the existing endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import secrets

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from superlily_contracts import AgentDispatchIn

from .main import run_attempt


_bearer = HTTPBearer(auto_error=False)


def create_app() -> FastAPI:
    def secret_value(name: str, file_name: str) -> str:
        path = os.getenv(file_name, "")
        if path:
            return Path(path).read_text(encoding="utf-8").strip()
        return os.getenv(name, "")

    core_url = os.getenv("SUPERLILY_MODEL_PROVIDER_CORE_URL", "").rstrip("/")
    core_token = secret_value(
        "SUPERLILY_MODEL_PROVIDER_TOKEN",
        "SUPERLILY_MODEL_PROVIDER_TOKEN_FILE",
    )
    api_key = secret_value(
        "SUPERLILY_DEEPSEEK_API_KEY",
        "SUPERLILY_DEEPSEEK_API_KEY_FILE",
    )
    base_url = os.getenv("SUPERLILY_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    trigger_token = secret_value(
        "SUPERLILY_MODEL_PROVIDER_TRIGGER_TOKEN",
        "SUPERLILY_MODEL_PROVIDER_TRIGGER_TOKEN_FILE",
    )
    timeout_seconds = float(os.getenv("SUPERLILY_DEEPSEEK_TIMEOUT_SECONDS", "30"))
    app = FastAPI(title="Superlily DeepSeek Provider", version="1.0.0")
    app.state.inflight = {}

    async def authorize(
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not trigger_token
            or not secrets.compare_digest(credentials.credentials, trigger_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
            )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not all((core_url, core_token, api_key, trigger_token)):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="provider configuration unavailable",
            )
        return {"status": "ok"}

    @app.post("/v1/attempts", dependencies=[Depends(authorize)])
    async def attempt(payload: AgentDispatchIn) -> dict:
        key = (payload.target_type, payload.target_id)
        task = app.state.inflight.get(key)
        if task is None:
            task = asyncio.create_task(
                run_attempt(
                    payload.target_id,
                    tool_loop=payload.target_type == "tool_loop",
                    core_url=core_url,
                    core_token=core_token,
                    api_key=api_key,
                    base_url=base_url,
                    timeout_seconds=timeout_seconds,
                ),
                name=f"deepseek:{payload.target_type}:{payload.target_id}",
            )
            app.state.inflight[key] = task
        try:
            return await asyncio.shield(task)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {404, 409}:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="target is no longer accepting an attempt",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Core model-attempt exchange failed",
            ) from exc
        finally:
            if task.done():
                app.state.inflight.pop(key, None)

    return app


def serve(argv: list[str] | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(prog="superlily-deepseek-provider serve")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0
