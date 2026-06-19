from collections.abc import AsyncIterator
import os

import httpx
import pytest

from superlily_core.app import create_app
from superlily_core.settings import Settings


@pytest.fixture
async def app(tmp_path):
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        database_url = f"sqlite+aiosqlite:///{tmp_path / 'core.db'}"
    settings = Settings(
        database_url=database_url,
        admin_token="admin-secret",
        ingest_tokens={"lily-command": "lily-secret", "nekro-agent": "nekro-secret"},
        stale_after_seconds=90,
        raw_enabled=True,
        raw_max_bytes=8_192,
    )
    instance = create_app(settings)
    await instance.state.database.create_schema()
    yield instance
    await instance.state.database.drop_schema()
    await instance.state.database.dispose()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as active_client:
        yield active_client
