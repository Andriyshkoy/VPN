from collections.abc import Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

from . import async_session
from .repo import (
    BillingRepo,
    ConfigRepo,
    ServerRepo,
    TelegramUpdateRepo,
    TelegramUserActionRepo,
    UserRepo,
    VPNOperationRepo,
)


@dataclass(frozen=True, slots=True)
class Repositories(Mapping[str, object]):
    """Typed repository bundle with mapping compatibility for legacy callers."""

    users: UserRepo
    servers: ServerRepo
    configs: ConfigRepo
    billing: BillingRepo
    vpn_operations: VPNOperationRepo
    telegram_updates: TelegramUpdateRepo
    telegram_user_actions: TelegramUserActionRepo

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __iter__(self) -> Iterator[str]:
        return iter(
            (
                "users",
                "servers",
                "configs",
                "billing",
                "vpn_operations",
                "telegram_updates",
                "telegram_user_actions",
            )
        )

    def __len__(self) -> int:
        return 7


@asynccontextmanager
async def uow():
    async with async_session() as session, session.begin():
        yield Repositories(
            users=UserRepo(session),
            servers=ServerRepo(session),
            configs=ConfigRepo(session),
            billing=BillingRepo(session),
            vpn_operations=VPNOperationRepo(session),
            telegram_updates=TelegramUpdateRepo(session),
            telegram_user_actions=TelegramUserActionRepo(session),
        )
