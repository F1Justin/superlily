from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session:
            yield session

    async def ping(self) -> None:
        async with self.sessions() as session:
            await session.execute(text("SELECT 1"))

    async def create_schema(self) -> None:
        """Test helper; production schema changes go through Alembic."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def drop_schema(self) -> None:
        """Test helper for disposable databases only."""
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
