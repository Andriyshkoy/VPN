from contextlib import asynccontextmanager

from . import async_session
from .repo import (
    BillingSettingsRepo,
    ConfigRepo,
    ServerRepo,
    TransactionRepo,
    UserRepo,
)


@asynccontextmanager
async def uow():
    async with async_session() as session, session.begin():
        yield {
            "users": UserRepo(session),
            "servers": ServerRepo(session),
            "configs": ConfigRepo(session),
            "billing_settings": BillingSettingsRepo(session),
            "transactions": TransactionRepo(session),
        }
