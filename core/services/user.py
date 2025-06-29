from __future__ import annotations

from typing import Callable

from core.exceptions import UserNotFoundError

from .models import Config, User


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
                raise UserNotFoundError(f"User with ID {user_id} not found")

            configs = await repos["configs"].list(owner_id=user_id)
            for cfg in configs:
                if not cfg.suspended:
                    await repos["configs"].suspend(cfg.id)

            await repos["users"].delete(id=user_id)
            return True

    async def get(self, user_id: int) -> User | None:
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                return None
            return User.from_orm(user)

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        username: str | None = None,
        tg_id: int | None = None,
    ) -> list[User]:
        """Return users filtered by the provided parameters."""
        filters: dict[str, object] = {}
        if username is not None:
            filters["username"] = username
        if tg_id is not None:
            filters["tg_id"] = tg_id

        async with self._uow() as repos:
            users = await repos["users"].list(
                limit=limit, offset=offset, **filters
            )
            return [User.from_orm(u) for u in users]

    async def update(self, user_id: int, **fields) -> User | None:
        """Update a user and return the updated instance or ``None``."""
        async with self._uow() as repos:
            user = await repos["users"].update(user_id, **fields)
            return User.from_orm(user) if user else None

    async def get_with_configs(self, user_id: int) -> tuple[User | None, list[Config]]:
        """Return a user and all their configs."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                return None, []
            configs = await repos["configs"].list(owner_id=user_id)
            return User.from_orm(user), [Config.from_orm(c) for c in configs]

    async def get_referrals(self, user_id: int, limit: int = 10, offset: int = 0) -> list[User]:
        """Get all users referred by a specific user."""
        async with self._uow() as repos:
            referrals = await repos["users"].get_referrals(user_id, limit, offset)
            return [User.from_orm(u) for u in referrals]

    async def count_referrals(self, user_id: int) -> int:
        """Count the number of users referred by a specific user."""
        async with self._uow() as repos:
            return await repos["users"].count_referrals(user_id)

    async def get_refferer(self, user_id: int) -> User | None:
        """Get the user who referred the specified user."""
        async with self._uow() as repos:
            refferer = await repos["users"].get_refferer(user_id)
            return User.from_orm(refferer) if refferer else None
