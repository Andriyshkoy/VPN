from __future__ import annotations
from typing import Any, Mapping

from core.db.models import User


class UserService:
    """Business‑logic helpers around **users**."""

    def __init__(self, repos: Mapping[str, Any]) -> None:
        self._users = repos["users"]

    async def get_or_create_user(self, tg_id: int, **kw) -> User:
        return await self._users.get_or_create(tg_id, **kw)

    async def get(self, user_id: int) -> User | None:
        return await self._users.get(id=user_id)
