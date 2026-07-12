from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from inspect import Parameter, signature
from typing import Callable, Sequence

from core.config import settings
from core.domain import VPNOperationKind, VPNOperationStatus, VPNState
from core.exceptions import (
    APIConfigurationError,
    APIConflictError,
    APIConnectionError,
    APINotFoundError,
    APIRequestRejectedError,
    ConfigNotFoundError,
    InvalidOperationError,
    ServerNotFoundError,
    UserNotFoundError,
)

from .api_gateway import APIGateway
from .models import Config

logger = logging.getLogger(__name__)

_NON_TERMINAL_STATUSES = {
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.RUNNING.value,
    VPNOperationStatus.FAILED.value,
}
_ACTIVATING_KINDS = {
    VPNOperationKind.PROVISION.value,
    VPNOperationKind.UNSUSPEND.value,
}
_TARGET_BY_KIND = {
    VPNOperationKind.PROVISION.value: VPNState.ACTIVE.value,
    VPNOperationKind.SUSPEND.value: VPNState.SUSPENDED.value,
    VPNOperationKind.UNSUSPEND.value: VPNState.ACTIVE.value,
    VPNOperationKind.REVOKE.value: VPNState.REVOKED.value,
}
_KIND_BY_TARGET = {
    VPNState.ACTIVE.value: VPNOperationKind.UNSUSPEND.value,
    VPNState.SUSPENDED.value: VPNOperationKind.SUSPEND.value,
    VPNState.REVOKED.value: VPNOperationKind.REVOKE.value,
}


@dataclass(frozen=True)
class _ConfigContext:
    config_id: int
    name: str
    owner_id: int
    server_id: int
    server_ip: str
    server_port: int
    server_api_key: str
    operation_id: str
    kind: str
    payload: dict
    lease_token: str | None = None
    attempts: int = 0


class ConfigService:
    """Application service for leased, recoverable VPN lifecycle operations."""

    def __init__(
        self,
        uow: Callable,
        *,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = 120,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 300,
    ) -> None:
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if lease_seconds <= 0:
            raise ValueError("VPN operation lease must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds < retry_base_seconds:
            raise ValueError("Invalid VPN operation retry backoff")
        self._lease_for = timedelta(seconds=lease_seconds)
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    @staticmethod
    def _validate_display_name(value: str) -> str:
        if not isinstance(value, str):
            raise InvalidOperationError("Configuration display name must be text")
        value = value.strip()
        if not value or len(value) > 128:
            raise InvalidOperationError(
                "Configuration display name must contain 1 to 128 characters"
            )
        return value

    @staticmethod
    def _ensure_provisioning_enabled() -> None:
        if settings.maintenance_mode or not settings.provisioning_enabled:
            raise InvalidOperationError("VPN provisioning is temporarily disabled")

    @staticmethod
    async def _call_mutation(method, *args, operation_id: str, **kwargs):
        """Forward the durable operation ID when an adapter supports it."""

        parameters = signature(method).parameters.values()
        supports_operation_id = any(
            parameter.name == "operation_id" or parameter.kind is Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_operation_id:
            kwargs["operation_id"] = operation_id
        return await method(*args, **kwargs)

    async def create_config(
        self,
        *,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
    ) -> Config:
        operation_id = str(uuid.uuid4())
        async with self._uow() as repos:
            context = await self.prepare_config(
                repos=repos,
                operation_id=operation_id,
                server_id=server_id,
                owner_id=owner_id,
                name=name,
                display_name=display_name,
                use_password=use_password,
            )
        return await self.execute_prepared(context)

    async def prepare_config(
        self,
        *,
        repos,
        operation_id: str,
        server_id: int,
        owner_id: int,
        name: str,
        display_name: str,
        use_password: bool = False,
    ) -> _ConfigContext:
        """Stage provision in the caller's transaction without doing network I/O."""

        self._ensure_provisioning_enabled()
        display_name = self._validate_display_name(display_name)
        if not isinstance(name, str) or not name or len(name) > 128:
            raise InvalidOperationError("Invalid internal configuration name")
        try:
            uuid.UUID(operation_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidOperationError("Invalid VPN operation ID") from exc

        # Serialise provisioning with Manager endpoint mutation/deletion.
        server = await repos["servers"].get_for_update(server_id)
        if not server:
            raise ServerNotFoundError(f"Server {server_id} not found")
        user = await repos["users"].get(id=owner_id)
        if not user:
            raise UserNotFoundError(f"User {owner_id} not found")

        cfg = await repos["configs"].create(
            server_id,
            owner_id,
            name,
            display_name,
            desired_state=VPNState.ACTIVE.value,
            actual_state=VPNState.PROVISIONING.value,
            operation_id=operation_id,
        )
        await repos["vpn_operations"].create(
            operation_id=operation_id,
            config_id=cfg.id,
            config_name=cfg.name,
            server_id=server.id,
            owner_id=owner_id,
            kind=VPNOperationKind.PROVISION.value,
            payload={"use_password": bool(use_password)},
            next_attempt_at=self._now(),
        )
        return _ConfigContext(
            config_id=cfg.id,
            name=cfg.name,
            owner_id=owner_id,
            server_id=server.id,
            server_ip=server.ip,
            server_port=server.port,
            server_api_key=server.api_key,
            operation_id=operation_id,
            kind=VPNOperationKind.PROVISION.value,
            payload={"use_password": bool(use_password)},
        )

    async def execute_prepared(self, context: _ConfigContext) -> Config:
        """Execute a provision intent after its surrounding transaction commits."""

        await self._execute(context.operation_id)
        result = await self.get(context.config_id)
        if result is None:  # pragma: no cover - defensive invariant
            raise ConfigNotFoundError(f"Config with ID {context.config_id} not found")
        return result

    async def download_config(self, config_id: int) -> bytes:
        context = await self._load_config_context(config_id)
        async with APIGateway(
            context.server_ip, context.server_port, context.server_api_key
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
            return Config.from_orm(cfg) if cfg else None

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
            return [Config.from_orm(c) for c in configs]

    async def list_suspended(self, *, owner_id: int | None = None) -> Sequence[Config]:
        async with self._uow() as repos:
            configs = await repos["configs"].get_suspended(owner_id=owner_id)
            return [Config.from_orm(c) for c in configs]

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
            configs = await repos["configs"].list(limit=limit, offset=offset, **filters)
            return [Config.from_orm(c) for c in configs]

    async def list_blocked(self, server_id: int) -> Sequence[str]:
        async with self._uow() as repos:
            server = await repos["servers"].get(id=server_id)
            if not server:
                raise ServerNotFoundError(f"Server {server_id} not found")
            ip, port, api_key = server.ip, server.port, server.api_key
        async with APIGateway(ip, port, api_key) as api:
            return await api.list_blocked()

    async def reconcile(self, *, limit: int = 100) -> dict[int, str]:
        """Fairly claim and execute a page of due durable operations."""

        if limit <= 0:
            raise ValueError("Reconciliation limit must be positive")
        excluded_kinds: tuple[str, ...] = ()
        if settings.maintenance_mode or not settings.provisioning_enabled:
            excluded_kinds = tuple(_ACTIVATING_KINDS)
        async with self._uow() as repos:
            due = await repos["vpn_operations"].list_due(
                now=self._now(),
                limit=limit,
                exclude_kinds=excluded_kinds,
            )
            operations = [(op.operation_id, op.config_id) for op in due]

        results: dict[int, str] = {}
        for operation_id, config_id in operations:
            if config_id is None:
                continue
            try:
                results[config_id] = await self._execute(operation_id)
            except Exception as exc:
                logger.exception(
                    "VPN reconciliation failed",
                    extra={"config_id": config_id, "operation_id": operation_id},
                )
                results[config_id] = f"failed:{type(exc).__name__}"
        return results

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

    async def execute_operations(
        self,
        operation_ids: Sequence[str],
        *,
        owner_id: int | None = None,
    ) -> int:
        """Best-effort execution after durable intents have committed."""

        completed = 0
        for operation_id in operation_ids:
            try:
                status = await self._execute(operation_id)
            except Exception:
                logger.exception(
                    "VPN entitlement side effect failed; durable intent retained",
                    extra={"operation_id": operation_id, "owner_id": owner_id},
                )
            else:
                completed += int(status == VPNOperationStatus.SUCCEEDED.value)
        return completed

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
            if cfg.actual_state == desired_state and current.attempts == 0:
                return None
            return await self._create_transition_locked(
                repos,
                cfg,
                desired_state=desired_state,
                kind=kind,
                now=not_before,
            )

        if cfg.actual_state == desired_state:
            return None
        if cfg.actual_state == VPNState.PROVISIONING.value:
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

    async def _execute(self, operation_id: str) -> str:
        context, status = await self._claim_context(operation_id)
        if context is None:
            return status

        target_state = _TARGET_BY_KIND.get(context.kind)
        if target_state is None:  # defensive: persisted operations are untrusted data
            exc = InvalidOperationError(f"Unknown VPN operation: {context.kind}")
            await self._mark_failure(context, exc, rejected=True)
            raise exc

        try:
            async with APIGateway(
                context.server_ip,
                context.server_port,
                context.server_api_key,
            ) as api:
                if context.kind == VPNOperationKind.PROVISION.value:
                    try:
                        await self._call_mutation(
                            api.create_client,
                            context.name,
                            use_password=bool(
                                context.payload.get("use_password", False)
                            ),
                            operation_id=context.operation_id,
                        )
                    except (APIConflictError, APIConnectionError) as exc:
                        try:
                            # The current Manager may return 500 when a retry
                            # finds the client created by an earlier ambiguous
                            # attempt. A downloadable config proves convergence.
                            await api.download_config(context.name)
                        except (APINotFoundError, APIConnectionError):
                            raise exc
                elif context.kind == VPNOperationKind.SUSPEND.value:
                    try:
                        await self._call_mutation(
                            api.suspend_client,
                            context.name,
                            operation_id=context.operation_id,
                        )
                    except APIConnectionError as exc:
                        blocked = await api.list_blocked()
                        if context.name not in blocked:
                            raise exc
                elif context.kind == VPNOperationKind.UNSUSPEND.value:
                    try:
                        await self._call_mutation(
                            api.unsuspend_client,
                            context.name,
                            operation_id=context.operation_id,
                        )
                    except APIConnectionError as exc:
                        blocked = await api.list_blocked()
                        if context.name in blocked:
                            raise exc
                elif context.kind == VPNOperationKind.REVOKE.value:
                    try:
                        await self._call_mutation(
                            api.revoke_client,
                            context.name,
                            operation_id=context.operation_id,
                        )
                    except APINotFoundError:
                        pass
                    except APIConnectionError as exc:
                        try:
                            await api.download_config(context.name)
                        except APINotFoundError:
                            pass
                        except APIConnectionError:
                            raise exc
                        else:
                            raise exc
        except asyncio.CancelledError:
            await asyncio.shield(
                self._mark_failure(
                    context,
                    APIConnectionError("VPN operation was cancelled"),
                    rejected=False,
                )
            )
            raise
        except APIConfigurationError as exc:
            await self._mark_failure(context, exc, rejected=True)
            raise
        except APIRequestRejectedError as exc:
            await self._mark_failure(context, exc, rejected=True)
            raise
        except APIConnectionError as exc:
            await self._mark_failure(context, exc, rejected=False)
            raise
        except Exception as exc:
            await self._mark_failure(context, exc, rejected=False)
            raise

        return await self._complete_success(context, target_state)

    async def _claim_context(
        self, operation_id: str
    ) -> tuple[_ConfigContext | None, str]:
        now = self._now()
        lease_token = str(uuid.uuid4())
        async with self._uow() as repos:
            operation = await repos["vpn_operations"].get(operation_id=operation_id)
            if operation is None:
                return None, "missing"
            if operation.kind in _ACTIVATING_KINDS and (
                settings.maintenance_mode or not settings.provisioning_enabled
            ):
                return None, "deferred:provisioning_disabled"
            if operation.config_id is None:
                await repos["vpn_operations"].mark_superseded(
                    operation_id,
                    now=now,
                )
                return None, VPNOperationStatus.SUPERSEDED.value

            cfg = await repos["configs"].get_for_update(operation.config_id)
            if cfg is None or cfg.operation_id != operation_id:
                await repos["vpn_operations"].mark_superseded(
                    operation_id,
                    now=now,
                )
                return None, VPNOperationStatus.SUPERSEDED.value

            claimed = await repos["vpn_operations"].claim(
                operation_id,
                lease_token=lease_token,
                now=now,
                lease_for=self._lease_for,
            )
            if claimed is None:
                return None, operation.status
            return (
                _ConfigContext(
                    config_id=cfg.id,
                    name=claimed.config_name,
                    owner_id=cfg.owner_id,
                    server_id=cfg.server_id,
                    server_ip=cfg.server.ip,
                    server_port=cfg.server.port,
                    server_api_key=cfg.server.api_key,
                    operation_id=claimed.operation_id,
                    kind=claimed.kind,
                    payload=claimed.payload,
                    lease_token=lease_token,
                    attempts=claimed.attempts,
                ),
                VPNOperationStatus.RUNNING.value,
            )

    async def _complete_success(
        self, context: _ConfigContext, target_state: str
    ) -> str:
        now = self._now()
        follow_up: str | None = None
        async with self._uow() as repos:
            cfg = await repos["configs"].get_for_update(context.config_id)
            completed = await repos["vpn_operations"].mark_succeeded(
                context.operation_id,
                lease_token=self._required_lease(context),
                now=now,
            )
            if completed is None:
                return VPNOperationStatus.SUPERSEDED.value
            if cfg is None or cfg.operation_id != context.operation_id:
                return VPNOperationStatus.SUCCEEDED.value

            if target_state == VPNState.REVOKED.value:
                await repos["configs"].delete_if_operation(
                    context.config_id,
                    operation_id=context.operation_id,
                )
            else:
                cfg = await repos["configs"].complete_transition(
                    context.config_id,
                    operation_id=context.operation_id,
                    actual_state=target_state,
                )
                if cfg and cfg.desired_state != target_state:
                    follow_kind = _KIND_BY_TARGET.get(cfg.desired_state)
                    if follow_kind:
                        follow_up = await self._plan_transition_locked(
                            repos,
                            cfg,
                            desired_state=cfg.desired_state,
                            kind=follow_kind,
                            now=now,
                        )

        if follow_up:
            try:
                await self._execute(follow_up)
            except Exception:
                # The original side effect succeeded. The follow-up remains durable
                # and must not be misreported as a failed provision to billing.
                logger.exception(
                    "Follow-up VPN entitlement remains pending",
                    extra={
                        "operation_id": follow_up,
                        "predecessor_operation_id": context.operation_id,
                    },
                )
        return VPNOperationStatus.SUCCEEDED.value

    async def _mark_failure(
        self,
        context: _ConfigContext,
        exc: Exception,
        *,
        rejected: bool,
    ) -> str:
        now = self._now()
        message = f"{type(exc).__name__}: {exc}"
        failed_state = (
            VPNState.FAILED.value
            if rejected and context.kind == VPNOperationKind.PROVISION.value
            else None
        )
        async with self._uow() as repos:
            cfg = await repos["configs"].get_for_update(context.config_id)
            if rejected:
                operation = await repos["vpn_operations"].mark_rejected(
                    context.operation_id,
                    message,
                    lease_token=self._required_lease(context),
                    now=now,
                )
                status = VPNOperationStatus.REJECTED.value
            elif context.attempts >= settings.vpn_operation_max_attempts:
                operation = await repos["vpn_operations"].mark_exhausted(
                    context.operation_id,
                    message,
                    lease_token=self._required_lease(context),
                    now=now,
                )
                status = VPNOperationStatus.EXHAUSTED.value
            else:
                next_attempt = now + timedelta(
                    seconds=self._retry_delay(context.attempts)
                )
                operation = await repos["vpn_operations"].mark_failed(
                    context.operation_id,
                    message,
                    lease_token=self._required_lease(context),
                    now=now,
                    next_attempt_at=next_attempt,
                )
                status = VPNOperationStatus.FAILED.value
            if operation is None:
                return VPNOperationStatus.SUPERSEDED.value
            if cfg is not None and cfg.operation_id == context.operation_id:
                await repos["configs"].fail_transition(
                    context.config_id,
                    operation_id=context.operation_id,
                    error=message,
                    actual_state=failed_state,
                )
            return status

    async def _load_config_context(self, config_id: int) -> _ConfigContext:
        async with self._uow() as repos:
            cfg = await repos["configs"].get(id=config_id, joined_load=["server"])
            if not cfg:
                raise ConfigNotFoundError(f"Config with ID {config_id} not found")
            operation = None
            if cfg.operation_id:
                operation = await repos["vpn_operations"].get(
                    operation_id=cfg.operation_id
                )
            return _ConfigContext(
                config_id=cfg.id,
                name=cfg.name,
                owner_id=cfg.owner_id,
                server_id=cfg.server_id,
                server_ip=cfg.server.ip,
                server_port=cfg.server.port,
                server_api_key=cfg.server.api_key,
                operation_id=cfg.operation_id or str(uuid.uuid4()),
                kind=operation.kind if operation else "read",
                payload=operation.payload if operation else {},
            )

    def _retry_delay(self, attempts: int) -> int:
        exponent = min(max(attempts - 1, 0), 30)
        return min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2**exponent),
        )

    def _now(self) -> datetime:
        value = self._clock()
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )

    @staticmethod
    def _required_lease(context: _ConfigContext) -> str:
        if not context.lease_token:  # pragma: no cover - internal invariant
            raise RuntimeError("Claimed VPN operation has no lease token")
        return context.lease_token
