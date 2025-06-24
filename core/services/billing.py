from __future__ import annotations

from decimal import Decimal
from typing import Callable

from core.exceptions import InsufficientBalanceError, UserNotFoundError

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
        self._cost = Decimal(per_config_cost)
        self._config_service = ConfigService(uow)

    async def top_up(self, user_id: int, amount: float) -> User:
        """Increase user's balance by ``amount`` and return the updated user."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            new_balance = user.balance + Decimal(amount)
            user = await repos["users"].update(user_id, balance=new_balance)

        if new_balance > 0:
            await self._config_service.unsuspend_all(user_id)

        return User.from_orm(user)

    async def charge_all(self) -> dict[User, Decimal]:
        """Charge all users for their active configurations.

        Returns a mapping of updated users to the amount charged."""
        async with self._uow() as repos:
            db_users = await repos["users"].list()

        charged: dict[User, Decimal] = {}
        for db_user in db_users:
            async with self._uow() as repos:
                configs = await repos["configs"].get_active(owner_id=db_user.id)
                charge = Decimal(len(configs)) * self._cost
                if not charge:
                    continue
                new_balance = db_user.balance - charge
                updated = await repos["users"].update(db_user.id, balance=new_balance)

            if new_balance <= 0:
                await self._config_service.suspend_all(db_user.id)

            charged[User.from_orm(updated)] = charge
        return charged

    async def withdraw(self, user_id: int, amount: float) -> User:
        """Deduct ``amount`` from user's balance and return updated user."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")
            if user.balance < Decimal(amount):
                raise InsufficientBalanceError("Insufficient balance")
            new_balance = user.balance - Decimal(amount)
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
    ) -> "Config":  # type: ignore[valid-type] # noqa
        """Create config and charge ``creation_cost`` on success."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=owner_id)
            if not user:
                raise UserNotFoundError(f"User with ID {owner_id} not found")
            if user.balance <= Decimal(creation_cost):
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
