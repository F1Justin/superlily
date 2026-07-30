import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .database import Database
from .control_routes import (
    descriptor_router,
    provider_router,
    rollout_router,
    router as control_router,
)
from .routes import router
from .settings import Settings
from .tool_artifact_service import reap_expired_artifacts
from .tool_execution_service import reap_expired_attempts
from .tool_invocation_service import reap_expired_invocations
from .agent_product_service import advance_agent_product, reap_agent_deliveries


logger = logging.getLogger(__name__)


class _EmptyLeaseAccessFilter(logging.Filter):
    """只隐藏成功的空 lease 轮询；真实 lease 和全部错误仍保留。"""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (
            '"POST /v1/tool-executions/lease HTTP/' in message
            and message.rstrip().endswith(" 204")
        )


def _install_access_log_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _EmptyLeaseAccessFilter) for item in access_logger.filters):
        access_logger.addFilter(_EmptyLeaseAccessFilter())


async def _run_tool_reaper(app: FastAPI, database: Database) -> None:
    """独立回收 artifact 与执行账本；任一失败不阻塞另一条清理线。"""

    while True:
        settings: Settings = app.state.settings
        if settings.artifact_enabled:
            try:
                async with database.sessions() as session:
                    await reap_expired_artifacts(session, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tool artifact reaper iteration failed")
        if settings.tool_execution_mode == "canary":
            try:
                async with database.sessions() as session:
                    await reap_expired_attempts(session)
                    await reap_expired_invocations(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("tool execution reaper iteration failed")
        await asyncio.sleep(settings.tool_reaper_interval_seconds)


async def _run_agent_product(app: FastAPI, database: Database) -> None:
    """Resume durable product interactions without coupling command ingress to models."""

    while True:
        settings: Settings = app.state.settings
        if settings.agent_product_mode == "canary":
            try:
                await advance_agent_product(database, settings)
                async with database.sessions() as session:
                    await reap_agent_deliveries(session)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent product coordinator iteration failed")
        await asyncio.sleep(settings.agent_coordinator_interval_seconds)


def create_app(settings: Settings | None = None) -> FastAPI:
    _install_access_log_filter()
    active_settings = settings or Settings.from_env()
    database = Database(active_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reaper = asyncio.create_task(
            _run_tool_reaper(app, database),
            name="superlily-tool-reaper",
        )
        agent_product = asyncio.create_task(
            _run_agent_product(app, database),
            name="superlily-agent-product",
        )
        try:
            yield
        finally:
            for task in (reaper, agent_product):
                task.cancel()
            for task in (reaper, agent_product):
                with suppress(asyncio.CancelledError):
                    await task
            await database.dispose()

    app = FastAPI(
        title="Lily Core",
        version="0.2.0",
        description="Fail-open observability spine for Lily and Nekro",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.database = database

    @app.middleware("http")
    async def control_security_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/control/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        if request.url.path.startswith(
            ("/v1/tool-executions", "/v1/tool-artifacts", "/v1/render-", "/v1/agent-")
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def redact_control_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/v1/control/"):
            return JSONResponse(status_code=422, content={"detail": "invalid control request"})
        if request.url.path.startswith("/v1/agent-"):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid agent request"},
            )
        if request.url.path.startswith(("/v1/tool-executions", "/v1/tool-artifacts", "/v1/render-")):
            return JSONResponse(
                status_code=422,
                content={"detail": "invalid provider execution request"},
            )
        return await request_validation_exception_handler(request, exc)

    app.include_router(router)
    app.include_router(control_router)
    app.include_router(descriptor_router)
    app.include_router(provider_router)
    app.include_router(rollout_router)
    return app


app = create_app()
