import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .database import Database
from .control_routes import descriptor_router, router as control_router
from .routes import router
from .settings import Settings
from .tool_execution_service import reap_expired_attempts
from .tool_invocation_service import reap_expired_invocations


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
    """只在可执行模式回收过期调用；异常记日志，不能拖垮 Core。"""

    while True:
        settings: Settings = app.state.settings
        try:
            if settings.tool_execution_mode in {"canary", "enforce"}:
                async with database.sessions() as session:
                    await reap_expired_attempts(session)
                    await reap_expired_invocations(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("tool execution reaper iteration failed")
        await asyncio.sleep(settings.tool_reaper_interval_seconds)


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
        try:
            yield
        finally:
            reaper.cancel()
            with suppress(asyncio.CancelledError):
                await reaper
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
        return response

    @app.exception_handler(RequestValidationError)
    async def redact_control_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith("/v1/control/"):
            return JSONResponse(status_code=422, content={"detail": "invalid control request"})
        return await request_validation_exception_handler(request, exc)

    app.include_router(router)
    app.include_router(control_router)
    app.include_router(descriptor_router)
    return app


app = create_app()
