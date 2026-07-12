from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.db.models import User

from .base import BaseRepo


class UserRepo(BaseRepo[User]):
    model = User

    async def get_for_update(self, user_id: int) -> User | None:
        """Lock an account while a dependent config/delete intent is staged."""

        return await self.session.scalar(
            select(self.model).where(self.model.id == user_id).with_for_update()
        )

    async def set_telegram_delivery_status(
        self,
        tg_id: int,
        *,
        delivery_status: str,
        error: str | None = None,
        observed_at: datetime | None = None,
    ) -> User | None:
        allowed = {"active", "blocked", "deactivated", "permanent_failure"}
        if delivery_status not in allowed:
            raise ValueError("invalid Telegram delivery status")
        observed_at = observed_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        else:
            observed_at = observed_at.astimezone(timezone.utc)
        terminal = delivery_status != "active"
        stmt = (
            update(self.model)
            .where(
                self.model.tg_id == tg_id,
                or_(
                    self.model.telegram_delivery_status_updated_at.is_(None),
                    self.model.telegram_delivery_status_updated_at <= observed_at,
                ),
            )
            .values(
                telegram_delivery_status=delivery_status,
                telegram_blocked_at=(datetime.now(timezone.utc) if terminal else None),
                telegram_last_delivery_error=(error[:4000] if error else None),
                telegram_delivery_status_updated_at=observed_at,
            )
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(self, tg_id: int, **kwargs) -> User:
        user, _ = await self.get_or_create_with_status(tg_id, **kwargs)
        return user

    async def get_or_create_with_status(
        self, tg_id: int, **kwargs
    ) -> tuple[User, bool]:
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
            # If user already exists and new username is provided, update it
            if "username" in kwargs and kwargs["username"] != user.username:
                user.username = kwargs["username"]
                return await self.update(user.id, username=user.username), False
            return user, False

        # If user does not exist, create a new one
        if "ref_id" in kwargs and kwargs["ref_id"] is not None:
            # If ref_id is provided, set referred_by_id to the user with that ID
            ref_user = await self.get(tg_id=kwargs["ref_id"])
            if ref_user:
                kwargs["referred_by_id"] = ref_user.id
            del kwargs["ref_id"]

        candidate = self.model(tg_id=tg_id, **kwargs)
        try:
            # The savepoint keeps the surrounding Unit of Work usable when a
            # duplicate Telegram update races this insert.
            async with self.session.begin_nested():
                self.session.add(candidate)
                await self.session.flush()
            return candidate, True
        except IntegrityError:
            existing = await self.get(tg_id=tg_id)
            if existing is None:
                raise
            if "username" in kwargs and kwargs["username"] != existing.username:
                return (
                    await self.update(existing.id, username=kwargs["username"]),
                    False,
                )
            return existing, False

    async def search_by_username(self, query: str, limit: int = 20) -> Sequence[User]:
        """
        Search users by username using a case-insensitive partial match.

        Args:
            query: The search term to look for in usernames
            limit: Maximum number of results to return (default: 20)

        Returns:
            Sequence of matching user objects
        """
        if not query:
            return []

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

    async def get_referrals(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> Sequence[User]:
        """
        Get all users referred by a specific user.

        Args:
            user_id: ID of the user whose referrals are to be fetched

        Returns:
            Sequence of users referred by the specified user
        """
        stmt = (
            select(self.model)
            .where(self.model.referred_by_id == user_id)
            .offset(offset)
            .limit(limit)
        )
        result = await self.session.scalars(stmt)
        return result.all()

    async def count_referrals(self, user_id: int) -> int:
        """
        Count the number of users referred by a specific user.

        Args:
            user_id: ID of the user whose referrals are to be counted

        Returns:
            Number of users referred by the specified user
        """
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.referred_by_id == user_id)
        )
        return await self.session.scalar(stmt)

    async def get_refferer(self, user_id: int) -> User | None:
        """
        Get the user who referred the specified user.

        Args:
            user_id: ID of the user to find the referrer for

        Returns:
            User object of the referrer or None if not found
        """
        user = await self.get(id=user_id)
        if user and user.referred_by_id:
            return await self.get(id=user.referred_by_id)
        return None
