import asyncio
from contextlib import asynccontextmanager, suppress
import logging

from fastapi import FastAPI

from .database import Database
from .routes import router
from .settings import Settings
from .tool_execution_service import reap_expired_attempts
from .tool_invocation_service import reap_expired_invocations


logger = logging.getLogger(__name__)


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
    app.include_router(router)
    return app


app = create_app()
