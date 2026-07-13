from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import selectinload

from core.db.models import VPN_Config
from core.domain import VPNState

from .base import BaseRepo


class ConfigRepo(BaseRepo[VPN_Config]):
    model = VPN_Config

    async def get_for_update(self, config_id: int) -> VPN_Config | None:
        """Lock a config while a new remote intent is being registered."""

        stmt = (
            select(self.model)
            .where(self.model.id == config_id)
            .options(selectinload(self.model.server))
            .with_for_update()
        )
        return await self.session.scalar(stmt)

    async def list_owner_for_update(self, owner_id: int) -> Sequence[VPN_Config]:
        """Lock an owner's configs before publishing a batch entitlement."""

        stmt = (
            select(self.model)
            .where(self.model.owner_id == owner_id)
            .options(selectinload(self.model.server))
            .order_by(self.model.id)
            .with_for_update()
        )
        return (await self.session.scalars(stmt)).all()

    async def get_active(self, owner_id: int = None) -> Sequence[VPN_Config]:
        """
        Get all active (not suspended) VPN configurations.

        Args:
            owner_id: Optional ID of the owner to filter by

        Returns:
            Sequence of active VPN configurations
        """
        stmt = select(self.model).where(
            self.model.actual_state == VPNState.ACTIVE.value
        )
        if owner_id is not None:
            stmt = stmt.where(self.model.owner_id == owner_id)
        return (await self.session.scalars(stmt.order_by(self.model.id))).all()

    async def get_suspended(self, owner_id: int = None) -> Sequence[VPN_Config]:
        """
        Get all suspended VPN configurations.

        Args:
            owner_id: Optional ID of the owner to filter by

        Returns:
            Sequence of suspended VPN configurations
        """
        stmt = select(self.model).where(
            self.model.actual_state == VPNState.SUSPENDED.value
        )
        if owner_id is not None:
            stmt = stmt.where(self.model.owner_id == owner_id)
        return (await self.session.scalars(stmt.order_by(self.model.id))).all()

    async def suspend(self, config_id: int) -> VPN_Config:
        """
        Suspend a VPN configuration by its ID.

        Args:
            config_id: ID of the configuration to suspend

        Returns:
            Updated VPN configuration or None if not found
        """
        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(
                suspended=True,
                suspended_at=datetime.now(timezone.utc),
                desired_state=VPNState.SUSPENDED.value,
                actual_state=VPNState.SUSPENDED.value,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def unsuspend(self, config_id: int) -> VPN_Config:
        """
        Remove suspension from a VPN configuration.

        Args:
            config_id: ID of the configuration to unsuspend

        Returns:
            Updated VPN configuration or None if not found
        """
        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(
                suspended=False,
                suspended_at=None,
                desired_state=VPNState.ACTIVE.value,
                actual_state=VPNState.ACTIVE.value,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()

    async def suspend_all(self, owner_id: int) -> int:
        """Suspend all active configs for a given user."""
        stmt = (
            update(self.model)
            .where(self.model.owner_id == owner_id, self.model.suspended.is_(False))
            .values(
                suspended=True,
                suspended_at=datetime.now(timezone.utc),
                desired_state=VPNState.SUSPENDED.value,
                actual_state=VPNState.SUSPENDED.value,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def unsuspend_all(self, owner_id: int) -> int:
        """Unsuspend all configs for a given user."""
        stmt = (
            update(self.model)
            .where(self.model.owner_id == owner_id, self.model.suspended.is_(True))
            .values(
                suspended=False,
                suspended_at=None,
                desired_state=VPNState.ACTIVE.value,
                actual_state=VPNState.ACTIVE.value,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.rowcount

    async def create(
        self,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        *,
        desired_state: str = VPNState.ACTIVE.value,
        actual_state: str = VPNState.ACTIVE.value,
        operation_id: str | None = None,
    ) -> VPN_Config:
        """
        Create a new VPN configuration.

        Args:
            server_id: ID of the server to associate with
            owner_id: ID of the owner
            name: Name of the configuration
            use_password: Whether to use password authentication

        Returns:
            Created VPN configuration
        """
        cfg = self.model(
            name=name,
            server_id=server_id,
            owner_id=owner_id,
            display_name=display_name,
            desired_state=desired_state,
            actual_state=actual_state,
            operation_id=operation_id,
        )
        return await self.add(cfg)

    async def begin_transition(
        self,
        config_id: int,
        *,
        desired_state: str,
        operation_id: str,
    ) -> VPN_Config | None:
        """Persist intent before calling the remote manager."""

        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(
                desired_state=desired_state,
                operation_id=operation_id,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_desired_state(
        self,
        config_id: int,
        *,
        desired_state: str,
    ) -> VPN_Config | None:
        """Update entitlement without pretending that Manager already converged."""

        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(
                desired_state=desired_state,
                last_error=None,
                updated_at=datetime.now(timezone.utc),
            )
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def complete_transition(
        self,
        config_id: int,
        *,
        operation_id: str,
        actual_state: str,
    ) -> VPN_Config | None:
        values: dict[str, object] = {
            "actual_state": actual_state,
            "last_error": None,
            "updated_at": datetime.now(timezone.utc),
        }
        if actual_state == VPNState.SUSPENDED.value:
            values.update(
                suspended=True,
                suspended_at=datetime.now(timezone.utc),
            )
        elif actual_state == VPNState.ACTIVE.value:
            values.update(suspended=False, suspended_at=None)

        stmt = (
            update(self.model)
            .where(
                self.model.id == config_id,
                self.model.operation_id == operation_id,
            )
            .values(**values)
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete_if_operation(self, config_id: int, *, operation_id: str) -> int:
        """Delete only when the completing revoke still owns the config intent."""

        stmt = delete(self.model).where(
            self.model.id == config_id,
            self.model.operation_id == operation_id,
        )
        result = await self.session.execute(stmt)
        return result.rowcount

    async def fail_transition(
        self,
        config_id: int,
        *,
        operation_id: str,
        error: str,
        actual_state: str | None = None,
    ) -> VPN_Config | None:
        values: dict[str, object] = {
            "last_error": error[:4000],
            "updated_at": datetime.now(timezone.utc),
        }
        if actual_state is not None:
            values["actual_state"] = actual_state
        stmt = (
            update(self.model)
            .where(
                self.model.id == config_id,
                self.model.operation_id == operation_id,
            )
            .values(**values)
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_drifted(self, *, limit: int = 100) -> Sequence[VPN_Config]:
        """Return configurations whose desired state has not converged."""

        stmt = (
            select(self.model)
            .where(
                or_(
                    self.model.desired_state != self.model.actual_state,
                    self.model.actual_state.in_(
                        [VPNState.PROVISIONING.value, VPNState.FAILED.value]
                    ),
                )
            )
            .order_by(self.model.id)
            .limit(limit)
        )
        return (await self.session.scalars(stmt)).all()

    async def update_display_name(
        self, config_id: int, new_name: str
    ) -> VPN_Config | None:
        """Update display name and return updated row."""
        stmt = (
            update(self.model)
            .where(self.model.id == config_id)
            .values(display_name=new_name)
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()
