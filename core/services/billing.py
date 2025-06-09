from __future__ import annotations

from typing import Callable, Sequence

from core.exceptions import UserNotFoundError, InsufficientBalanceError

from .config import ConfigService
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
        self._config_service = ConfigService(uow)

    async def top_up(self, user_id: int, amount: float) -> User:
        """Increase user's balance by ``amount`` and return the updated user."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            new_balance = user.balance + amount
            user = await repos["users"].update(user_id, balance=new_balance)

        if new_balance > 0:
            await self._config_service.unsuspend_all(user_id)

        return User.from_orm(user)

    async def charge_all(self) -> None:
        """Charge all users for their active configurations."""
        async with self._uow() as repos:
            users: Sequence[User] = await repos["users"].list()

        for user in users:
            async with self._uow() as repos:
                configs = await repos["configs"].get_active(owner_id=user.id)
                charge = len(configs) * self._cost
                if charge:
                    new_balance = user.balance*100 - charge*100  # To avoid float precision issues
                    new_balance = new_balance / 100
                    await repos["users"].update(user.id, balance=new_balance)
                else:
                    continue

            if new_balance <= 0 and charge:
                await self._config_service.suspend_all(user.id)

    async def withdraw(self, user_id: int, amount: float) -> User:
        """Deduct ``amount`` from user's balance and return updated user."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            if user.balance < amount:
                raise InsufficientBalanceError("Insufficient balance")
            new_balance = user.balance - amount
            user = await repos["users"].update(user_id, balance=new_balance)

        if new_balance <= 0:
            await self._config_service.suspend_all(user_id)

        return User.from_orm(user)

    async def create_paid_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        creation_cost: float,
        use_password: bool = False,
    ) -> "Config":
        """Create config and charge ``creation_cost`` on success."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=owner_id)
            if not user:
                raise UserNotFoundError(f"User with ID {owner_id} not found")
            if user.balance < creation_cost:
                raise InsufficientBalanceError("Insufficient balance")

        cfg = await self._config_service.create_config(
            server_id=server_id,
            owner_id=owner_id,
            name=name,
            display_name=display_name,
            use_password=use_password,
        )

        await self.withdraw(owner_id, creation_cost)
        return cfg
