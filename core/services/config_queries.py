from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from core.config import settings
from core.domain import VPNOperationKind, VPNOperationStatus, VPNState
from core.exceptions import (
    ConfigNotFoundError,
    InvalidOperationError,
    ServerNotFoundError,
)

from ._config_shared import (
    _KIND_BY_TARGET,
    _NON_TERMINAL_STATUSES,
)
from .models import Config

logger = logging.getLogger("core.services.config")


class ConfigQueriesEntitlementsMixin:
    """Read configs and publish durable entitlement transitions."""

    async def download_config(self, config_id: int) -> bytes:
        context = await self._load_config_context(config_id)
        async with self._create_gateway(
            context.server_ip,
            context.server_port,
            context.server_api_key,
        ) as api:
            return await api.download_config(context.name)

    async def revoke_config(self, config_id: int) -> None:
        operation_id = await self._start_transition(
            config_id,
            desired_state=VPNState.REVOKED.value,
            kind=VPNOperationKind.REVOKE.value,
        )
        if operation_id:
            await self._execute(operation_id)

    async def suspend_config(self, config_id: int) -> Config:
        operation_id = await self._start_transition(
            config_id,
            desired_state=VPNState.SUSPENDED.value,
            kind=VPNOperationKind.SUSPEND.value,
        )
        if operation_id:
            await self._execute(operation_id)
        result = await self.get(config_id)
        if result is None:  # pragma: no cover - defensive invariant
            raise ConfigNotFoundError(f"Config with ID {config_id} not found")
        return result

    async def unsuspend_config(self, config_id: int) -> Config:
        self._ensure_provisioning_enabled()
        operation_id = await self._start_transition(
            config_id,
            desired_state=VPNState.ACTIVE.value,
            kind=VPNOperationKind.UNSUSPEND.value,
        )
        if operation_id:
            await self._execute(operation_id)
        result = await self.get(config_id)
        if result is None:  # pragma: no cover - defensive invariant
            raise ConfigNotFoundError(f"Config with ID {config_id} not found")
        return result

    async def rename_config(self, config_id: int, new_name: str) -> Config:
        new_name = self._validate_display_name(new_name)
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id)
            if not cfg:
                raise ConfigNotFoundError(f"Config with ID {config_id} not found")
            cfg = await repos["configs"].update_display_name(config_id, new_name)
            return Config.from_orm(cfg)

    async def get(self, config_id: int) -> Config | None:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id)
            if cfg is None:
                return None
            operation = None
            if cfg.operation_id:
                operation = await repos["vpn_operations"].get(
                    operation_id=cfg.operation_id
                )
            return Config.from_orm(cfg, operation=operation)

    async def suspend_all(self, owner_id: int) -> int:
        return await self._apply_entitlement(
            owner_id,
            desired_state=VPNState.SUSPENDED.value,
            kind=VPNOperationKind.SUSPEND.value,
        )

    async def unsuspend_all(self, owner_id: int) -> int:
        self._ensure_provisioning_enabled()
        return await self._apply_entitlement(
            owner_id,
            desired_state=VPNState.ACTIVE.value,
            kind=VPNOperationKind.UNSUSPEND.value,
        )

    async def list_active(self, *, owner_id: int | None = None) -> Sequence[Config]:
        async with self._uow() as repos:
            configs = await repos["configs"].get_active(owner_id=owner_id)
            return await self._with_operation_snapshots(repos, configs)

    async def list_suspended(
        self,
        *,
        owner_id: int | None = None,
    ) -> Sequence[Config]:
        async with self._uow() as repos:
            configs = await repos["configs"].get_suspended(owner_id=owner_id)
            return await self._with_operation_snapshots(repos, configs)

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        server_id: int | None = None,
        owner_id: int | None = None,
        suspended: bool | None = None,
    ) -> Sequence[Config]:
        filters: dict[str, object] = {}
        if server_id is not None:
            filters["server_id"] = server_id
        if owner_id is not None:
            filters["owner_id"] = owner_id
        if suspended is not None:
            filters["suspended"] = suspended
        async with self._uow() as repos:
            configs = await repos["configs"].list(
                limit=limit,
                offset=offset,
                **filters,
            )
            return await self._with_operation_snapshots(repos, configs)

    @staticmethod
    async def _with_operation_snapshots(repos, configs) -> list[Config]:
        operation_ids = [cfg.operation_id for cfg in configs if cfg.operation_id]
        operations = await repos["vpn_operations"].list_by_operation_ids(operation_ids)
        by_id = {operation.operation_id: operation for operation in operations}
        return [
            Config.from_orm(
                cfg,
                operation=by_id.get(cfg.operation_id),
            )
            for cfg in configs
        ]

    async def list_blocked(self, server_id: int) -> Sequence[str]:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if not server:
                raise ServerNotFoundError(f"Server {server_id} not found")
            ip, port, api_key = server.ip, server.port, server.api_key
        async with self._create_gateway(ip, port, api_key) as api:
            return await api.list_blocked()

    async def _apply_entitlement(
        self,
        owner_id: int,
        *,
        desired_state: str,
        kind: str,
    ) -> int:
        """Publish every config intent atomically, then perform remote work."""

        async with self._uow() as repos:
            planned = await self.prepare_entitlement(
                repos=repos,
                owner_id=owner_id,
                desired_state=desired_state,
                kind=kind,
            )
        return await self.execute_operations(planned, owner_id=owner_id)

    async def prepare_entitlement(
        self,
        *,
        repos,
        owner_id: int,
        desired_state: str,
        kind: str,
    ) -> list[str]:
        """Stage every owner's latest intent in the caller's transaction."""

        now = self._now()
        planned: list[str] = []
        configs = await repos["configs"].list_owner_for_update(owner_id)
        for cfg in configs:
            try:
                operation_id = await self._plan_transition_locked(
                    repos,
                    cfg,
                    desired_state=desired_state,
                    kind=kind,
                    now=now,
                )
            except InvalidOperationError:
                logger.warning(
                    "VPN config cannot accept batch entitlement",
                    extra={
                        "config_id": cfg.id,
                        "owner_id": owner_id,
                        "desired_state": desired_state,
                    },
                    exc_info=True,
                )
                continue
            if operation_id and operation_id not in planned:
                planned.append(operation_id)
        return planned

    async def prepare_drift_repairs(
        self,
        expected_targets: Mapping[int, str],
    ) -> dict[int, str]:
        """Atomically stage explicitly approved, non-destructive drift repairs.

        The caller must provide the desired state observed during its audit.
        Rows are re-locked and compared here so a concurrent entitlement
        change cannot be repaired toward a stale target. Only ACTIVE and
        SUSPENDED are supported; provisioning and revocation remain manual.
        """

        if not expected_targets:
            return {}
        if settings.maintenance_mode:
            raise InvalidOperationError("VPN drift repair is disabled in maintenance")

        normalized: dict[int, str] = {}
        for config_id, desired_state in expected_targets.items():
            if isinstance(config_id, bool) or not isinstance(config_id, int):
                raise InvalidOperationError("Invalid VPN config ID for drift repair")
            if desired_state not in {
                VPNState.ACTIVE.value,
                VPNState.SUSPENDED.value,
            }:
                raise InvalidOperationError(
                    "Only active/suspended VPN drift can be repaired automatically"
                )
            normalized[config_id] = desired_state

        planned: dict[int, str] = {}
        async with self._uow() as repos:
            for config_id in sorted(normalized):
                expected_state = normalized[config_id]
                cfg = await repos["configs"].get_for_update(config_id)
                if cfg is None:
                    raise ConfigNotFoundError(f"Config with ID {config_id} not found")
                if cfg.desired_state != expected_state:
                    raise InvalidOperationError(
                        "VPN config entitlement changed after drift audit"
                    )
                if cfg.actual_state not in {
                    VPNState.ACTIVE.value,
                    VPNState.SUSPENDED.value,
                }:
                    raise InvalidOperationError(
                        "VPN config lifecycle state requires manual reconciliation"
                    )
                if expected_state == VPNState.ACTIVE.value:
                    self._ensure_provisioning_enabled()
                operation_id = await self._plan_transition_locked(
                    repos,
                    cfg,
                    desired_state=expected_state,
                    kind=_KIND_BY_TARGET[expected_state],
                    now=self._now(),
                    force=True,
                )
                if operation_id:
                    planned[config_id] = operation_id
        return planned

    async def _start_transition(
        self,
        config_id: int,
        *,
        desired_state: str,
        kind: str,
    ) -> str | None:
        async with self._uow() as repos:
            cfg = await repos["configs"].get_for_update(config_id)
            if not cfg:
                raise ConfigNotFoundError(f"Config with ID {config_id} not found")
            return await self._plan_transition_locked(
                repos,
                cfg,
                desired_state=desired_state,
                kind=kind,
                now=self._now(),
            )

    async def _plan_transition_locked(
        self,
        repos,
        cfg,
        *,
        desired_state: str,
        kind: str,
        now: datetime,
        force: bool = False,
    ) -> str | None:
        """Publish latest intent while the config row is locked.

        PROVISION is a prerequisite rather than an entitlement and is therefore
        allowed to finish before a queued suspend/revoke. Opposite ordinary
        intents are fenced as SUPERSEDED; a compensating operation is delayed
        until an in-flight predecessor's lease ends.
        """

        current = None
        if cfg.operation_id:
            current = await repos["vpn_operations"].get(operation_id=cfg.operation_id)
        current_non_terminal = bool(
            current and current.status in _NON_TERMINAL_STATUSES
        )

        if (
            current_non_terminal
            and current.kind == VPNOperationKind.REVOKE.value
            and kind != VPNOperationKind.REVOKE.value
        ):
            raise InvalidOperationError("A revocation cannot be superseded")
        if cfg.actual_state == VPNState.FAILED.value and kind in {
            VPNOperationKind.SUSPEND.value,
            VPNOperationKind.UNSUSPEND.value,
        }:
            raise InvalidOperationError(
                "A failed provision must be retried or revoked before activation changes"
            )

        if (
            kind == VPNOperationKind.REVOKE.value
            and cfg.actual_state == VPNState.FAILED.value
            and current is not None
            and current.kind == VPNOperationKind.PROVISION.value
            and current.status == VPNOperationStatus.REJECTED.value
        ):
            # A definitive provision rejection never confirmed a remote
            # credential. Preserve operation/refund history but avoid the
            # legacy Manager's unknown-client 500 path.
            await repos["configs"].delete_if_operation(
                cfg.id,
                operation_id=current.operation_id,
            )
            return None

        await repos["configs"].set_desired_state(
            cfg.id,
            desired_state=desired_state,
        )

        if current_non_terminal:
            # Provisioning must settle before applying the latest entitlement. Its
            # success path schedules the follow-up using desired_state.
            if current.kind == VPNOperationKind.PROVISION.value:
                return current.operation_id
            if current.kind == kind:
                return current.operation_id

            old_lease_until = self._aware(current.lease_until)
            not_before = (
                old_lease_until
                if current.status == VPNOperationStatus.RUNNING.value
                and old_lease_until is not None
                and old_lease_until > now
                else now
            )
            superseded = await repos["vpn_operations"].mark_superseded(
                current.operation_id,
                now=now,
            )
            if superseded is None:
                raise InvalidOperationError("VPN operation changed concurrently")

            # A never-claimed opposite operation had no remote side effect. If
            # local actual state already equals the new intent, compensation is
            # unnecessary.
            if (
                cfg.actual_state == desired_state
                and current.attempts == 0
                and not force
            ):
                return None
            return await self._create_transition_locked(
                repos,
                cfg,
                desired_state=desired_state,
                kind=kind,
                now=not_before,
            )

        if cfg.actual_state == desired_state and not force:
            return None
        if cfg.actual_state == VPNState.PROVISIONING.value:
            if kind == VPNOperationKind.REVOKE.value:
                # A terminal/missing provision may have succeeded remotely
                # before its response was lost. Revoke is the safe cleanup:
                # the Manager path is idempotent and treats not-found as done.
                return await self._create_transition_locked(
                    repos,
                    cfg,
                    desired_state=desired_state,
                    kind=kind,
                    now=now,
                )
            raise InvalidOperationError("Provisioning operation is missing or terminal")
        return await self._create_transition_locked(
            repos,
            cfg,
            desired_state=desired_state,
            kind=kind,
            now=now,
        )

    async def _create_transition_locked(
        self,
        repos,
        cfg,
        *,
        desired_state: str,
        kind: str,
        now: datetime,
    ) -> str:
        operation_id = str(uuid.uuid4())
        await repos["configs"].begin_transition(
            cfg.id,
            desired_state=desired_state,
            operation_id=operation_id,
        )
        await repos["vpn_operations"].create(
            operation_id=operation_id,
            config_id=cfg.id,
            config_name=cfg.name,
            server_id=cfg.server_id,
            owner_id=cfg.owner_id,
            kind=kind,
            next_attempt_at=now,
        )
        return operation_id
