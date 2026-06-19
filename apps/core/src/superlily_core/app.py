from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import Database
from .routes import router
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or Settings.from_env()
    database = Database(active_settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await database.dispose()

    app = FastAPI(
        title="Lily Core",
        version="0.1.0",
        description="Fail-open observability spine for Lily and Nekro",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.state.database = database
    app.include_router(router)
    return app


app = create_app()

