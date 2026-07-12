from __future__ import annotations

from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from core.db.models.ledger import LedgerEntry, LedgerKind
from core.db.models.payment import ProviderPayment
from core.db.models.user import User as UserModel
from core.db.repo.billing import to_money
from core.domain import VPNOperationKind, VPNState
from core.exceptions import InvalidOperationError, UserNotFoundError

from .config import ConfigService
from .models import Config, User


class UserService:
    """High level operations for **users**."""

    def __init__(self, uow: Callable):
        """Store callable returning UnitOfWork."""
        self._uow = uow
        self._config_service = ConfigService(uow)

    async def register(self, tg_id: int, **kw) -> User:
        initial_balance = to_money(kw.pop("balance", 0) or 0)
        if initial_balance < 0:
            raise InvalidOperationError("Initial balance cannot be negative")
        async with self._uow() as repos:
            user, created = await repos["users"].get_or_create_with_status(tg_id, **kw)
            if not created and user.telegram_delivery_status != "active":
                user = await repos["users"].set_telegram_delivery_status(
                    tg_id,
                    delivery_status="active",
                )
            if initial_balance and created:
                movement = await repos["billing"].apply_balance_change(
                    user_id=user.id,
                    amount=initial_balance,
                    kind=LedgerKind.OPENING_BALANCE,
                    idempotency_key=f"opening-balance:user:{user.id}",
                    allow_negative_balance=False,
                    details={"source": "registration"},
                )
                user = movement.user
            return User.from_orm(user)

    async def delete(self, user_id: int) -> bool:
        async with self._uow() as repos:
            user = await repos["users"].get_for_update(user_id)
            if not user:
                raise UserNotFoundError(f"User with ID {user_id} not found")

            configs = await repos["configs"].list(owner_id=user_id)
            if configs:
                # Deleting the local owner before remote revocation creates
                # unmanaged credentials.  A dedicated deprovision use case can
                # be added to the admin UI later; until then fail closed.
                return False

            session = repos["users"].session
            financial_record = await session.scalar(
                select(LedgerEntry.id).where(LedgerEntry.user_id == user_id).limit(1)
            )
            payment_record = await session.scalar(
                select(ProviderPayment.id)
                .where(ProviderPayment.user_id == user_id)
                .limit(1)
            )
            if financial_record is not None or payment_record is not None:
                # Financial history is immutable. A future admin use case should
                # anonymize/disable this account instead of physically deleting it.
                return False

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
            users = await repos["users"].list(limit=limit, offset=offset, **filters)
            return [User.from_orm(u) for u in users]

    async def update(self, user_id: int, **fields) -> User | None:
        """Update a user while routing balance changes through the ledger."""

        requested_balance = fields.pop("balance", None)
        planned: list[str] = []
        async with self._uow() as repos:
            user = None
            if requested_balance is not None:
                session = repos["users"].session
                current = await session.scalar(
                    select(UserModel).where(UserModel.id == user_id).with_for_update()
                )
                if current is None:
                    return None
                target = to_money(requested_balance)
                delta = target - current.balance
                if delta:
                    movement = await repos["billing"].apply_balance_change(
                        user_id=user_id,
                        amount=delta,
                        kind=LedgerKind.ADMIN_ADJUSTMENT,
                        idempotency_key=f"admin-adjustment:{uuid4()}",
                        allow_negative_balance=False,
                        details={"target_balance": str(target)},
                    )
                    user = movement.user
                else:
                    user = current

            if fields:
                user = await repos["users"].update(user_id, **fields)
            elif user is None:
                user = await repos["users"].get(id=user_id)
            if requested_balance is not None and user is not None:
                desired_state, kind = (
                    (VPNState.ACTIVE.value, VPNOperationKind.UNSUSPEND.value)
                    if user.balance > 0
                    else (VPNState.SUSPENDED.value, VPNOperationKind.SUSPEND.value)
                )
                planned = await self._config_service.prepare_entitlement(
                    repos=repos,
                    owner_id=user_id,
                    desired_state=desired_state,
                    kind=kind,
                )
            result = User.from_orm(user) if user else None

        await self._config_service.execute_operations(planned, owner_id=user_id)
        return result

    async def get_with_configs(self, user_id: int) -> tuple[User | None, list[Config]]:
        """Return a user and all their configs."""
        async with self._uow() as repos:
            user = await repos["users"].get(id=user_id)
            if not user:
                return None, []
            configs = await repos["configs"].list(owner_id=user_id)
            return User.from_orm(user), [Config.from_orm(c) for c in configs]

    async def get_referrals(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[User]:
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
