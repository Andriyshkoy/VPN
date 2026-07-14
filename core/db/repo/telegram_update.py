from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.db.models.telegram_update import TelegramUpdateInbox
from core.domain.telegram import TelegramUpdateStatus

from .base import BaseRepo

_NON_TERMINAL_STATUSES = (
    TelegramUpdateStatus.PENDING.value,
    TelegramUpdateStatus.PROCESSING.value,
    TelegramUpdateStatus.FAILED.value,
)


class TelegramUpdateRepo(BaseRepo[TelegramUpdateInbox]):
    """Persistence and fenced leases for incoming Telegram updates."""

    model = TelegramUpdateInbox

    async def ingest(
        self,
        *,
        update_id: int,
        payload: dict,
        source: str,
        ordering_key: str,
    ) -> tuple[TelegramUpdateInbox, bool]:
        """Insert an update once and never replace the accepted payload."""

        existing = await self.get(update_id=update_id)
        if existing is not None:
            return existing, False

        if not ordering_key or len(ordering_key) > 80:
            raise ValueError("invalid Telegram update ordering key")
        row = self.model(
            update_id=update_id,
            payload=payload,
            source=source,
            ordering_key=ordering_key,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.get(update_id=update_id)
            if existing is None:
                raise
            return existing, False
        return row, True

    async def claim_next(
        self,
        *,
        lease_token: str,
        now: datetime,
        lease_for: timedelta,
        max_attempts: int,
    ) -> TelegramUpdateInbox | None:
        """Lease the oldest due update, recovering expired processing leases."""

        # A lower retry budget must not strand an already-failed row forever.
        # Preserve its diagnostic error while terminalizing it so its ordering
        # lane can continue. Processing rows still wait for lease expiry below.
        await self.session.execute(
            update(self.model)
            .where(
                self.model.status == TelegramUpdateStatus.FAILED.value,
                self.model.attempts >= max_attempts,
            )
            .values(
                status=TelegramUpdateStatus.DEAD.value,
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
        )

        # If the process died during the final allowed attempt there is no
        # handler left to mark the row terminal. Close that crash window before
        # looking for retryable work.
        await self.session.execute(
            update(self.model)
            .where(
                self.model.status == TelegramUpdateStatus.PROCESSING.value,
                self.model.attempts >= max_attempts,
                self.model.lease_until.is_not(None),
                self.model.lease_until <= now,
            )
            .values(
                status=TelegramUpdateStatus.DEAD.value,
                lease_token=None,
                lease_until=None,
                last_error="processing lease expired after final attempt",
                updated_at=now,
            )
        )

        due = and_(
            self.model.status.in_(
                (
                    TelegramUpdateStatus.PENDING.value,
                    TelegramUpdateStatus.FAILED.value,
                )
            ),
            self.model.attempts < max_attempts,
            self.model.next_attempt_at <= now,
        )
        expired = and_(
            self.model.status == TelegramUpdateStatus.PROCESSING.value,
            self.model.attempts < max_attempts,
            self.model.lease_until.is_not(None),
            self.model.lease_until <= now,
        )
        # Preserve Telegram/FSM ordering across retries within a pseudonymous
        # conversation lane. A failed N blocks N+1 in the same lane, while an
        # unrelated chat remains available. SKIP LOCKED lets replicas select a
        # different lane; the correlated predecessor check prevents them from
        # leapfrogging a locked head in this lane.
        earlier = aliased(self.model)
        earlier_in_lane = (
            select(earlier.id)
            .where(
                earlier.ordering_key == self.model.ordering_key,
                earlier.status.in_(_NON_TERMINAL_STATUSES),
                or_(
                    earlier.update_id < self.model.update_id,
                    and_(
                        earlier.update_id == self.model.update_id,
                        earlier.id < self.model.id,
                    ),
                ),
            )
            .exists()
        )
        candidate_id = await self.session.scalar(
            select(self.model.id)
            .where(or_(due, expired), ~earlier_in_lane)
            .order_by(self.model.update_id, self.model.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if candidate_id is None:
            return None

        stmt = (
            update(self.model)
            .where(self.model.id == candidate_id, or_(due, expired))
            .values(
                status=TelegramUpdateStatus.PROCESSING.value,
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

    async def terminalize_exhausted(
        self,
        *,
        now: datetime,
        max_attempts: int,
    ) -> list[tuple[TelegramUpdateInbox, str]]:
        """Close exhausted rows and return them for atomic safe audit append.

        ``claim_next`` retains equivalent guards for direct repository callers.
        The bot service calls this method first so auto-DEAD transitions and
        their immutable user-action event share one transaction.
        """

        terminalized: list[tuple[TelegramUpdateInbox, str]] = []
        failed = (
            update(self.model)
            .where(
                self.model.status == TelegramUpdateStatus.FAILED.value,
                self.model.attempts >= max_attempts,
            )
            .values(
                status=TelegramUpdateStatus.DEAD.value,
                lease_token=None,
                lease_until=None,
                updated_at=now,
            )
            .returning(self.model)
        )
        terminalized.extend(
            (row, "retry_budget_exhausted")
            for row in (await self.session.scalars(failed)).all()
        )

        expired = (
            update(self.model)
            .where(
                self.model.status == TelegramUpdateStatus.PROCESSING.value,
                self.model.attempts >= max_attempts,
                self.model.lease_until.is_not(None),
                self.model.lease_until <= now,
            )
            .values(
                status=TelegramUpdateStatus.DEAD.value,
                lease_token=None,
                lease_until=None,
                last_error="processing lease expired after final attempt",
                updated_at=now,
            )
            .returning(self.model)
        )
        terminalized.extend(
            (row, "lease_expired")
            for row in (await self.session.scalars(expired)).all()
        )
        return terminalized

    async def renew_lease(
        self,
        update_id: int,
        *,
        lease_token: str,
        now: datetime,
        lease_for: timedelta,
    ) -> bool:
        stmt = (
            update(self.model)
            .where(*self._owned_processing(update_id, lease_token))
            .values(lease_until=now + lease_for, updated_at=now)
        )
        return bool((await self.session.execute(stmt)).rowcount)

    async def mark_processed(
        self,
        update_id: int,
        *,
        lease_token: str,
        now: datetime,
    ) -> bool:
        stmt = (
            update(self.model)
            .where(*self._owned_processing(update_id, lease_token))
            .values(
                status=TelegramUpdateStatus.PROCESSED.value,
                # Keep the unique update_id as the dedupe receipt while
                # removing message/contact/payment PII as soon as handling is
                # durably acknowledged.
                payload={},
                lease_token=None,
                lease_until=None,
                last_error=None,
                updated_at=now,
                processed_at=now,
            )
        )
        return bool((await self.session.execute(stmt)).rowcount)

    async def mark_failed(
        self,
        update_id: int,
        *,
        lease_token: str,
        error: str,
        now: datetime,
        next_attempt_at: datetime,
        exhausted: bool,
    ) -> bool:
        values = {
            "status": (
                TelegramUpdateStatus.DEAD.value
                if exhausted
                else TelegramUpdateStatus.FAILED.value
            ),
            "lease_token": None,
            "lease_until": None,
            "last_error": error[:4000],
            "next_attempt_at": next_attempt_at,
            "updated_at": now,
        }
        if exhausted:
            # The caller already owns the in-memory payload needed for its
            # safe classification. Do not retain raw dead-letter contents.
            values["payload"] = {}
        stmt = (
            update(self.model)
            .where(*self._owned_processing(update_id, lease_token))
            .values(**values)
        )
        return bool((await self.session.execute(stmt)).rowcount)

    async def purge_terminal_before(
        self,
        *,
        processed_cutoff: datetime,
        dead_cutoff: datetime,
        limit: int = 1000,
    ) -> int:
        """Delete bounded old terminal history, never retryable work."""

        ids = (
            await self.session.scalars(
                select(self.model.id)
                .where(
                    or_(
                        and_(
                            self.model.status == TelegramUpdateStatus.PROCESSED.value,
                            self.model.processed_at.is_not(None),
                            self.model.processed_at < processed_cutoff,
                        ),
                        and_(
                            self.model.status == TelegramUpdateStatus.DEAD.value,
                            self.model.updated_at < dead_cutoff,
                        ),
                    )
                )
                .order_by(self.model.updated_at, self.model.id)
                .limit(limit)
            )
        ).all()
        if not ids:
            return 0
        result = await self.session.execute(
            delete(self.model).where(self.model.id.in_(ids))
        )
        return int(result.rowcount or 0)

    def _owned_processing(self, update_id: int, lease_token: str) -> tuple:
        return (
            self.model.update_id == update_id,
            self.model.status == TelegramUpdateStatus.PROCESSING.value,
            self.model.lease_token == lease_token,
        )
