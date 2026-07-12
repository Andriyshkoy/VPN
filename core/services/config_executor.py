from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta
from inspect import Parameter, signature

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
)

from ._config_shared import (
    _ACTIVATING_KINDS,
    _KIND_BY_TARGET,
    _TARGET_BY_KIND,
    _ConfigContext,
)

logger = logging.getLogger("core.services.config")


class ConfigLeasedExecutorMixin:
    """Claim, execute, fence, and retry durable VPN Manager operations."""

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
                    extra={
                        "config_id": config_id,
                        "operation_id": operation_id,
                    },
                )
                results[config_id] = f"failed:{type(exc).__name__}"
        return results

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
            async with self._create_gateway(
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
                            # A retry may find a client created by an earlier
                            # ambiguous attempt. Download proves convergence.
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
        self,
        operation_id: str,
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
        self,
        context: _ConfigContext,
        target_state: str,
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
                # The predecessor succeeded. The follow-up remains durable and
                # must not be misreported as a failed provision to billing.
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

    @staticmethod
    def _required_lease(context: _ConfigContext) -> str:
        if not context.lease_token:  # pragma: no cover - internal invariant
            raise RuntimeError("Claimed VPN operation has no lease token")
        return context.lease_token
