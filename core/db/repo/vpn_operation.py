from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import and_, case, or_, select, update

from core.db.models.vpn_operation import VPNOperation
from core.domain import VPNOperationStatus

from .base import BaseRepo

_CLAIMABLE_STATUSES = (
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.FAILED.value,
)
_NON_TERMINAL_STATUSES = (
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.RUNNING.value,
    VPNOperationStatus.FAILED.value,
)


class VPNOperationRepo(BaseRepo[VPNOperation]):
    model = VPNOperation

    async def create(
        self,
        *,
        operation_id: str,
        config_id: int | None,
        config_name: str,
        server_id: int,
        owner_id: int | None,
        kind: str,
        payload: dict | None = None,
        next_attempt_at: datetime | None = None,
    ) -> VPNOperation:
        values = {
            "operation_id": operation_id,
            "config_id": config_id,
            "config_name": config_name,
            "server_id": server_id,
            "owner_id": owner_id,
            "kind": kind,
            "payload": payload or {},
        }
        if next_attempt_at is not None:
            values["next_attempt_at"] = next_attempt_at
        return await self.add(self.model(**values))

    async def claim(
        self,
        operation_id: str,
        *,
        lease_token: str,
        now: datetime,
        lease_for: timedelta,
    ) -> VPNOperation | None:
        """Atomically lease one due operation to exactly one executor.

        A stale RUNNING operation is recoverable once its lease expires.  Every
        completion update is fenced by ``lease_token``, so a late worker cannot
        overwrite the result of a newer claim.
        """

        due_pending = and_(
            self.model.status.in_(_CLAIMABLE_STATUSES),
            or_(
                self.model.next_attempt_at.is_(None),
                self.model.next_attempt_at <= now,
            ),
        )
        stale_running = and_(
            self.model.status == VPNOperationStatus.RUNNING.value,
            self.model.lease_until.is_not(None),
            self.model.lease_until <= now,
        )
        stmt = (
            update(self.model)
            .where(
                self.model.operation_id == operation_id,
                or_(due_pending, stale_running),
            )
            .values(
                status=VPNOperationStatus.RUNNING.value,
                attempts=self.model.attempts + 1,
                lease_token=lease_token,
                lease_until=now + lease_for,
                last_error=None,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_succeeded(
        self,
        operation_id: str,
        *,
        lease_token: str,
        now: datetime,
    ) -> VPNOperation | None:
        stmt = (
            update(self.model)
            .where(*self._owned_running(operation_id, lease_token))
            .values(
                status=VPNOperationStatus.SUCCEEDED.value,
                last_error=None,
                lease_token=None,
                lease_until=None,
                updated_at=now,
                completed_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_failed(
        self,
        operation_id: str,
        error: str,
        *,
        lease_token: str,
        now: datetime,
        next_attempt_at: datetime,
    ) -> VPNOperation | None:
        stmt = (
            update(self.model)
            .where(*self._owned_running(operation_id, lease_token))
            .values(
                status=VPNOperationStatus.FAILED.value,
                last_error=error[:4000],
                next_attempt_at=next_attempt_at,
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_rejected(
        self,
        operation_id: str,
        error: str,
        *,
        lease_token: str,
        now: datetime,
    ) -> VPNOperation | None:
        stmt = (
            update(self.model)
            .where(*self._owned_running(operation_id, lease_token))
            .values(
                status=VPNOperationStatus.REJECTED.value,
                last_error=error[:4000],
                lease_token=None,
                lease_until=None,
                updated_at=now,
                completed_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_exhausted(
        self,
        operation_id: str,
        error: str,
        *,
        lease_token: str,
        now: datetime,
    ) -> VPNOperation | None:
        """Stop automatic retries once an ambiguous operation uses its budget."""

        stmt = (
            update(self.model)
            .where(*self._owned_running(operation_id, lease_token))
            .values(
                status=VPNOperationStatus.EXHAUSTED.value,
                last_error=error[:4000],
                lease_token=None,
                lease_until=None,
                updated_at=now,
                completed_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_superseded(
        self,
        operation_id: str,
        *,
        now: datetime,
    ) -> VPNOperation | None:
        """Fence a stale intent before publishing a newer desired state."""

        stmt = (
            update(self.model)
            .where(
                self.model.operation_id == operation_id,
                self.model.status.in_(_NON_TERMINAL_STATUSES),
            )
            .values(
                status=VPNOperationStatus.SUPERSEDED.value,
                lease_token=None,
                lease_until=None,
                updated_at=now,
                completed_at=now,
            )
            .execution_options(synchronize_session="fetch")
            .returning(self.model)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
        exclude_kinds: Sequence[str] | None = None,
    ) -> Sequence[VPNOperation]:
        """Return a fair page of due or lease-expired operations.

        Terminal rows never consume the reconciliation page. Ordering by the
        effective due time prevents old rejected records from starving later
        recoverable work.
        """

        now = now or datetime.now(timezone.utc)
        due_pending = and_(
            self.model.status.in_(_CLAIMABLE_STATUSES),
            or_(
                self.model.next_attempt_at.is_(None),
                self.model.next_attempt_at <= now,
            ),
        )
        stale_running = and_(
            self.model.status == VPNOperationStatus.RUNNING.value,
            self.model.lease_until.is_not(None),
            self.model.lease_until <= now,
        )
        effective_due = case(
            (
                self.model.status == VPNOperationStatus.RUNNING.value,
                self.model.lease_until,
            ),
            else_=self.model.next_attempt_at,
        )
        stmt = select(self.model).where(or_(due_pending, stale_running))
        if exclude_kinds:
            stmt = stmt.where(self.model.kind.not_in(tuple(exclude_kinds)))
        stmt = stmt.order_by(effective_due, self.model.id).limit(limit)
        return (await self.session.scalars(stmt)).all()

    async def list_retryable(self, *, limit: int = 100) -> Sequence[VPNOperation]:
        """Compatibility alias for callers migrating to due-time reconciliation."""

        return await self.list_due(limit=limit)

    async def list_by_operation_ids(
        self,
        operation_ids: Sequence[str],
    ) -> Sequence[VPNOperation]:
        """Load current operation snapshots for control-plane presentation."""

        ids = tuple(dict.fromkeys(operation_ids))
        if not ids:
            return []
        stmt = (
            select(self.model)
            .where(self.model.operation_id.in_(ids))
            .order_by(self.model.id)
        )
        return (await self.session.scalars(stmt)).all()

    def _owned_running(self, operation_id: str, lease_token: str) -> tuple:
        return (
            self.model.operation_id == operation_id,
            self.model.status == VPNOperationStatus.RUNNING.value,
            self.model.lease_token == lease_token,
        )
