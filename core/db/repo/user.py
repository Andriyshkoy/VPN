# core/db/repo/user.py

from typing import Sequence

from sqlalchemy import select, update

from core.db.models import User

from .base import BaseRepo


class UserRepo(BaseRepo[User]):
    model = User

    async def get_or_create(self, tg_id: int, **kwargs) -> User:
        """
        Get a user by Telegram ID or create them if they don't exist.

        Args:
            tg_id: Telegram ID of the user
            **kwargs: Additional attributes for the user if they need to be created

        Returns:
            Existing or newly created user object
        """
        user = await self.get(tg_id=tg_id)
        if user:
            return user
        return await self.add(self.model(tg_id=tg_id, **kwargs))

    async def search_by_username(self, query: str, limit: int = 20) -> Sequence[User]:
        """
        Search users by username using a case-insensitive partial match.

        Args:
            query: The search term to look for in usernames
            limit: Maximum number of results to return (default: 20)

        Returns:
            Sequence of matching user objects
        """
        stmt = (
            select(self.model)
            .where(self.model.username.ilike(f"%{query}%"))
            .limit(limit)
        )
        users = await self.session.scalars(stmt)
        return users.all()

    async def update(self, user_id: int, **kwargs) -> User:
        """Update a user and return the updated object."""
        stmt = (
            update(self.model)
            .where(self.model.id == user_id)
            .values(**kwargs)
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()
