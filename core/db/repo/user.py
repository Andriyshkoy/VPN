from typing import Sequence

from sqlalchemy import select

from core.db.models import User

from .base import BaseRepo


class UserRepo(BaseRepo[User]):
    model = User

    async def get_or_create(self, tg_id: int, **kwargs) -> User:
        user = await self.get(tg_id=tg_id)
        if user:
            return user
        return await self.add(User(tg_id=tg_id, **kwargs))

    async def search_by_username(self, query: str, limit: int = 20) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.username.ilike(f"%{query}%"))
            .limit(limit)
        )
        users = await self.session.scalars(stmt)
        return users.all()
