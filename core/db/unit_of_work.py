# core/db/unit_of_work.py

from contextlib import asynccontextmanager

from . import async_session
from .repo import ConfigRepo, ServerRepo, UserRepo


@asynccontextmanager
async def uow():
    async with async_session() as session, session.begin():
        yield {
            "users": UserRepo(session),
            "servers": ServerRepo(session),
            "configs": ConfigRepo(session)
        }
