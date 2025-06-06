from __future__ import annotations

from typing import Callable

from .models import User


class UserService:
    """High level operations for **users**."""

    def __init__(self, uow: Callable):
        """Store callable returning UnitOfWork."""
        self._uow = uow

    async def register(self, tg_id: int, **kw) -> User:
        async with self._uow() as repos:
            user = await repos["users"].get_or_create(tg_id, **kw)
            return User.from_orm(user)

    async def delete(self, user_id: int) -> bool:
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                return False

            configs = await repos["configs"].list(owner_id=user_id)
            for cfg in configs:
                if not cfg.suspended:
                    await repos["configs"].suspend(cfg.id)

            await repos["users"].delete(id=user_id)
            return True

    async def get(self, user_id: int) -> User | None:
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            return User.from_orm(user) if user else None

    async def list(self) -> list[User]:
        """Return all users."""
        async with self._uow() as repos:
            users = await repos["users"].list()
            return [User.from_orm(u) for u in users]
