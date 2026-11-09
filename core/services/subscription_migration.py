"""Reviewable, opt-in migration of product assignments; never provisions VPNs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select

from core.config import settings
from core.db.models.user import User
from core.exceptions import InvalidOperationError


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    user_id: int
    active_config_count: int
    plan_code: str | None
    status: str
    existing_subscription_id: str | None
    eligible: bool
    reason: str
    config_ids: tuple[int, ...]
    snapshot_hash: str


class SubscriptionMigrationService:
    def __init__(self, uow):
        self._uow = uow

    async def _candidate(self, repos, user_id, *, lock=False):
        configs = (
            await repos["configs"].list_owner_for_update(user_id)
            if lock
            else await repos["configs"].list(owner_id=user_id)
        )
        snapshot = sorted(
            (cfg.id, cfg.desired_state, cfg.actual_state) for cfg in configs
        )
        active = sum(
            desired == "active" and actual == "active"
            for _, desired, actual in snapshot
        )
        retained = [cfg for cfg in configs if cfg.desired_state != "revoked"]
        existing = await repos["subscriptions"].get_by_user(user_id, for_update=lock)
        status = "active" if active else "suspended" if retained else "no_configs"
        plan_code = "plus" if active >= 2 else "basic" if retained else None
        digest = hashlib.sha256(
            json.dumps(snapshot, separators=(",", ":")).encode()
        ).hexdigest()
        return MigrationCandidate(
            user_id=user_id,
            active_config_count=active,
            plan_code=plan_code,
            status=status,
            existing_subscription_id=existing.id if existing else None,
            eligible=existing is None and bool(retained),
            reason=(
                "already_assigned"
                if existing
                else (
                    "no_retained_configs"
                    if not retained
                    else "active_configs" if active else "keep_access_disabled"
                )
            ),
            config_ids=tuple(item[0] for item in snapshot),
            snapshot_hash=digest,
        )

    async def preview(self) -> tuple[MigrationCandidate, ...]:
        """Read only: does not create rows, flags, charges or remote operations."""
        async with self._uow() as repos:
            user_ids = (
                await repos["users"].session.scalars(select(User.id).order_by(User.id))
            ).all()
            return tuple([await self._candidate(repos, uid) for uid in user_ids])

    async def apply(self, manifest, *, source: str) -> tuple[str, ...]:
        """Apply an explicitly reviewed snapshot; reruns cannot remap accounts.

        This method is deliberately not exposed by an HTTP route or startup
        hook. Production execution requires a separately approved rollout.
        """
        if settings.billing_enabled:
            raise InvalidOperationError("Pause billing before subscription migration")
        if not isinstance(source, str) or not source.strip() or len(source) > 64:
            raise InvalidOperationError("Invalid migration source")
        manifest = tuple(manifest)
        if len({item.user_id for item in manifest}) != len(manifest):
            raise InvalidOperationError("Duplicate account in migration manifest")
        migrated = []
        async with self._uow() as repos:
            for approved in sorted(manifest, key=lambda item: item.user_id):
                user = await repos["users"].get_for_update(approved.user_id)
                if user is None:
                    raise InvalidOperationError("Migration account no longer exists")
                current = await self._candidate(repos, approved.user_id, lock=True)
                if current.existing_subscription_id:
                    row = await repos["subscriptions"].get_by_user(approved.user_id)
                    if (
                        row.migration_source == source
                        and row.migration_snapshot.get("snapshot_hash")
                        == approved.snapshot_hash
                    ):
                        migrated.append(row.id)
                        continue
                    if (
                        approved.existing_subscription_id
                        == current.existing_subscription_id
                    ):
                        continue
                    raise InvalidOperationError(
                        "Subscription was assigned since migration preview"
                    )
                if current != approved:
                    raise InvalidOperationError(
                        "Migration snapshot changed; review a fresh preview"
                    )
                if not current.eligible:
                    continue
                row = await repos["subscriptions"].create(
                    current.user_id,
                    current.plan_code,
                    status="pending" if current.status == "active" else current.status,
                    migration_source=source,
                    migration_snapshot={
                        "snapshot_hash": current.snapshot_hash,
                        "active_config_count": current.active_config_count,
                        "legacy_config_ids": list(current.config_ids),
                        "billing_enabled": False,
                        "credentials_provisioned": False,
                        "cutover_state": "pending",
                    },
                )
                # Finite wallet, immutable ledger, legacy credentials and both
                # manual flags are deliberately untouched.
                migrated.append(row.id)
        return tuple(migrated)
