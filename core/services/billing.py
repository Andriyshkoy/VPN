from __future__ import annotations

from typing import Callable, Sequence

from core.exceptions import UserNotFoundError

from .models import User


class BillingService:
    """Service that handles manual top-ups and periodic charges."""

    def __init__(self, uow: Callable, *, per_config_cost: float) -> None:
        """
        Initialize the billing service.
        :param uow: Unit of Work factory to manage database transactions.
        :param per_config_cost: Cost charged for each active configuration.
        """
        self._uow = uow
        self._cost = per_config_cost

    async def top_up(self, user_id: int, amount: float) -> User:
        """Increase user's balance by ``amount`` and return the updated user."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            new_balance = user.balance + amount
            user = await repos["users"].update(user_id, balance=new_balance)
            if new_balance > 0:
                await repos["configs"].unsuspend_all(user_id)
            return User.from_orm(user)

    async def charge_all(self) -> None:
        """Charge all users for their active configurations."""
        async with self._uow() as repos:
            users: Sequence[User] = await repos["users"].list()
            for user in users:
                configs = await repos["configs"].get_active(owner_id=user.id)
                charge = len(configs) * self._cost
                if charge:
                    new_balance = user.balance - charge
                    await repos["users"].update(user.id, balance=new_balance)
                    if new_balance <= 0:
                        await repos["configs"].suspend_all(user.id)
