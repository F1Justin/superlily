from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in request.app.state.database.session():
        session.info["settings"] = request.app.state.settings
        yield session
