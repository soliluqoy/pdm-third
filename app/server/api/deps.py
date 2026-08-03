"""Shared API dependencies."""
from fastapi import Depends

from server.db import async_session_factory


async def get_session():
    async with async_session_factory() as session:
        yield session


SessionDep = Depends(get_session)
