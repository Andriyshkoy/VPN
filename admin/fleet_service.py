from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from fastapi import Request
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError

import core.db as db
from core.config import settings
from core.db.models import (
    AdminAction,
    AdminAuditEvent,
    AdminUser,
    Server,
    VPN_Config,
    VPNServerStatus,
)
from core.db.unit_of_work import uow
from core.domain import AdminActionStatus, ServerLifecycleState, VPNState
from core.exceptions import APIGatewayError, InvalidOperationError, ServerNotFoundError
from core.services.api_gateway import APIGateway
from core.services.fleet_placement import (
    managed_config_condition,
    manager_readiness_decision,
)
from core.services.vpn_drift import VPNDriftService

from .fleet_schemas import (
    AdminServerActionRequest,
    AdminServerCreate,
    AdminServerUpdate,
)
from .security import AdminPrincipal, add_audit_event

logger = logging.getLogger(__name__)


class FleetIdempotencyConflict(InvalidOperationError):
    pass


class FleetOptimisticConflict(InvalidOperationError):
    pass


class AdminFleetRemoteError(RuntimeError):
    """A Manager call failed after its durable action was recorded."""


@dataclass(frozen=True, slots=True)
class _ManagerTarget:
    id: int
    ip: str
    port: int
    api_key: str
    manager_instance_id: str | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal | int | float | None) -> str:
    return f"{Decimal(value or 0):.2f}"


def _decimal_string(value: Decimal | int | None) -> str:
    return format(Decimal(value or 0), "f")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


class AdminFleetService:
    """Typed fleet reads and narrowly-scoped, durable administration actions."""

    _REMOTE_ACTIONS = {
        "health_check": "refresh_status",
        "refresh_health": "refresh_status",
        "refresh_status": "refresh_status",
        "refresh_inventory": "refresh_inventory",
        "audit_drift": "audit_drift",
    }
    _MUTATING_ACTIONS = {
        "set_accepting",
        "enable_new_configs",
        "disable_new_configs",
        "drain",
        "start_drain",
        "disable",
        "activate",
        "retire",
        "update_capacity",
    }

    def __init__(
        self,
        *,
        gateway_factory: Callable[..., APIGateway] | None = None,
        drift_service: VPNDriftService | None = None,
    ) -> None:
        self._gateway_factory = gateway_factory or APIGateway
        self._drift_service = drift_service or VPNDriftService(
            uow, gateway_factory=self._gateway_factory
        )

    async def list_servers(
        self,
        *,
        q: str | None = None,
        lifecycle_state: str | None = None,
        health_state: str | None = None,
        location: str | None = None,
        provider: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        conditions = []
        if q:
            pattern = f"%{q.strip()}%"
            conditions.append(
                or_(
                    Server.name.ilike(pattern),
                    Server.location.ilike(pattern),
                    Server.ip.ilike(pattern),
                    Server.host.ilike(pattern),
                    Server.public_endpoint.ilike(pattern),
                    Server.provider.ilike(pattern),
                )
            )
        if lifecycle_state:
            conditions.append(Server.lifecycle_state == lifecycle_state)
        if location:
            conditions.append(Server.location == location)
        if provider:
            conditions.append(Server.provider == provider)

        async with db.async_session() as session:
            if health_state:
                rows = (
                    await session.scalars(
                        select(Server).where(*conditions).order_by(Server.id)
                    )
                ).all()
                all_items = [await self._server_payload(session, row) for row in rows]
                unhealthy = {"unhealthy", "unreachable", "instance_mismatch"}
                items = [
                    item
                    for item in all_items
                    if (
                        item["health"] in unhealthy
                        if health_state == "unhealthy"
                        else item["health"] == health_state
                    )
                ]
                return {
                    "items": items[offset : offset + limit],
                    "total": len(items),
                    "limit": limit,
                    "offset": offset,
                }
            total = await session.scalar(
                select(func.count(Server.id)).where(*conditions)
            )
            rows = (
                await session.scalars(
                    select(Server)
                    .where(*conditions)
                    .order_by(Server.id)
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            items = [await self._server_payload(session, row) for row in rows]
        return {
            "items": items,
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
        }

    async def get_server(self, server_id: int) -> dict[str, Any] | None:
        async with db.async_session() as session:
            server = await session.get(Server, server_id)
            if server is None:
                return None
            return await self._server_payload(session, server, include_status=True)

    async def get_latest_status(self, server_id: int) -> dict[str, Any] | None:
        async with db.async_session() as session:
            server = await session.get(Server, server_id)
            if server is None:
                return None
            latest = await self._latest_status(session, server_id, kind="status")
            return await self._status_payload(session, server, latest)

    async def status_history(
        self,
        server_id: int,
        *,
        kind: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any] | None:
        async with db.async_session() as session:
            if await session.get(Server, server_id) is None:
                return None
            conditions = [VPNServerStatus.server_id == server_id]
            if kind:
                conditions.append(VPNServerStatus.kind == kind)
            total = await session.scalar(
                select(func.count(VPNServerStatus.id)).where(*conditions)
            )
            rows = (
                await session.scalars(
                    select(VPNServerStatus)
                    .where(*conditions)
                    .order_by(
                        VPNServerStatus.collected_at.desc(), VPNServerStatus.id.desc()
                    )
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return {
                "items": [self._status_record(row) for row in rows],
                "total": int(total or 0),
                "limit": limit,
                "offset": offset,
            }

    async def list_actions(
        self,
        *,
        server_id: int | None,
        status: str | None,
        kind: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        conditions = []
        if server_id is not None:
            conditions.append(AdminAction.server_id == server_id)
        if status:
            conditions.append(AdminAction.status == status)
        if kind:
            conditions.append(AdminAction.kind == kind)
        async with db.async_session() as session:
            total = await session.scalar(
                select(func.count(AdminAction.id)).where(*conditions)
            )
            rows = (
                await session.scalars(
                    select(AdminAction)
                    .where(*conditions)
                    .order_by(AdminAction.created_at.desc(), AdminAction.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return {
                "items": [await self._action_payload(session, row) for row in rows],
                "total": int(total or 0),
                "limit": limit,
                "offset": offset,
            }

    async def get_action(self, action_id: str) -> dict[str, Any] | None:
        async with db.async_session() as session:
            row = await session.scalar(
                select(AdminAction).where(AdminAction.action_id == action_id)
            )
            return await self._action_payload(session, row) if row else None

    async def recover_stale_actions(self) -> int:
        """Fail abandoned RUNNING actions after their bounded execution window.

        Remote reads are safe to retry with a new idempotency key. Marking an
        abandoned action terminal prevents a browser replay from waiting on a
        request that disappeared during a process crash.
        """

        cutoff = _utcnow().timestamp() - settings.admin_action_stale_seconds
        cutoff_at = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        async with db.async_session() as session, session.begin():
            rows = (
                await session.scalars(
                    select(AdminAction)
                    .where(
                        AdminAction.status.in_(
                            (
                                AdminActionStatus.PENDING.value,
                                AdminActionStatus.RUNNING.value,
                            )
                        ),
                        AdminAction.started_at.is_not(None),
                        AdminAction.started_at < cutoff_at,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
            now = _utcnow()
            for action in rows:
                previous_status = action.status
                action.status = AdminActionStatus.FAILED.value
                action.error_code = "stale_action_recovered"
                action.error_detail = (
                    "Action did not finish before its execution lease expired"
                )
                action.completed_at = now
                session.add(
                    AdminAuditEvent(
                        actor_user_id=action.actor_user_id,
                        action=f"server.{action.kind}.recovered",
                        target_type="server",
                        target_id=str(action.server_id) if action.server_id else None,
                        request_id=f"recovery:{action.action_id}",
                        correlation_id=f"recovery:{action.action_id}",
                        details={
                            "admin_action_id": action.action_id,
                            "previous_status": previous_status,
                            "outcome": "failed",
                            "error_code": "stale_action_recovered",
                        },
                    )
                )
            return len(rows)

    async def poll_server_statuses(self) -> dict[str, int]:
        """Refresh bounded Manager snapshots for every non-retired server.

        This is observation only: it never changes lifecycle, placement, VPN
        configs, or balances, and it deliberately does not manufacture admin
        actions. Instance IDs are learned once and mismatches remain visible.
        """

        async with db.async_session() as session:
            rows = (
                await session.scalars(
                    select(Server)
                    .where(Server.lifecycle_state != ServerLifecycleState.RETIRED.value)
                    .order_by(Server.id)
                )
            ).all()
            targets = [
                _ManagerTarget(
                    id=row.id,
                    ip=row.ip,
                    port=row.port,
                    api_key=row.api_key,
                    manager_instance_id=row.manager_instance_id,
                )
                for row in rows
            ]

        semaphore = asyncio.Semaphore(settings.admin_fleet_poll_concurrency)
        counts = {"checked": len(targets), "succeeded": 0, "failed": 0}
        counts_lock = asyncio.Lock()

        async def collect(target: _ManagerTarget) -> None:
            async with semaphore:
                try:
                    result = await self._refresh_status(target)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._persist_polled_status_failure(
                        target.id,
                        error_code=type(exc).__name__,
                    )
                    async with counts_lock:
                        counts["failed"] += 1
                    return
                await self._persist_polled_status(target.id, result)
                async with counts_lock:
                    counts["succeeded"] += 1

        await asyncio.gather(*(collect(target) for target in targets))
        return counts

    async def run_status_poller(self, stop: asyncio.Event) -> None:
        """Continuously collect status with bounded concurrency and jitter."""

        while not stop.is_set():
            try:
                result = await self.poll_server_statuses()
                logger.info(
                    "Fleet status poll completed checked=%s succeeded=%s failed=%s",
                    result["checked"],
                    result["succeeded"],
                    result["failed"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Fleet status poll cycle failed")
            interval = settings.admin_fleet_poll_interval_seconds
            delay = interval + random.random() * min(15, interval * 0.1)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _persist_polled_status(
        self,
        server_id: int,
        result: dict[str, Any],
    ) -> None:
        async with db.async_session() as session, session.begin():
            server = await session.get(Server, server_id)
            if (
                server is None
                or server.lifecycle_state == ServerLifecycleState.RETIRED.value
            ):
                return
            row = VPNServerStatus(
                server_id=server_id,
                kind="status",
                success=True,
                snapshot=result["snapshot"],
                manager_observed_at=result.get("manager_observed_at"),
                manager_version=result.get("manager_version"),
                manager_instance_id=result.get("manager_instance_id"),
                inventory_revision=result.get("inventory_revision"),
            )
            session.add(row)
            observed_instance = result.get("manager_instance_id")
            if observed_instance and server.manager_instance_id is None:
                server.manager_instance_id = observed_instance
            await session.flush()
            await self._prune_status_history(session, server_id)

    async def _persist_polled_status_failure(
        self,
        server_id: int,
        *,
        error_code: str,
    ) -> None:
        async with db.async_session() as session, session.begin():
            server = await session.get(Server, server_id)
            if (
                server is None
                or server.lifecycle_state == ServerLifecycleState.RETIRED.value
            ):
                return
            session.add(
                VPNServerStatus(
                    server_id=server_id,
                    kind="status",
                    success=False,
                    snapshot={},
                    error_code=error_code[:96],
                )
            )
            await session.flush()
            await self._prune_status_history(session, server_id)

    async def create_server(
        self,
        *,
        request: Request,
        principal: AdminPrincipal,
        data: AdminServerCreate,
    ) -> dict[str, Any]:
        values = data.model_dump()
        # The API schema rejects unsafe values; keep the invariant here too so
        # non-HTTP callers cannot register a placement-ready endpoint.
        values["lifecycle_state"] = ServerLifecycleState.DISABLED.value
        values["accepts_new_configs"] = False
        async with db.async_session() as session, session.begin():
            server = Server(**values)
            session.add(server)
            await session.flush()
            add_audit_event(
                session,
                request,
                action="server.created",
                actor_user_id=principal.user_id,
                target_type="server",
                target_id=server.id,
                details={
                    "name": server.name,
                    "lifecycle_state": server.lifecycle_state,
                    "api_key_configured": True,
                },
            )
        async with db.async_session() as session:
            persisted = await session.get(Server, server.id)
            assert persisted is not None
            return await self._server_payload(session, persisted)

    async def update_server(
        self,
        server_id: int,
        *,
        request: Request,
        principal: AdminPrincipal,
        data: AdminServerUpdate,
    ) -> dict[str, Any]:
        supplied = data.model_dump(exclude_unset=True)
        expected_version = supplied.pop("expected_version")
        clear_max_configs = supplied.pop("clear_max_configs", False)
        supplied = {
            key: value
            for key, value in supplied.items()
            if value is not None or key in {"provider", "public_endpoint"}
        }
        if clear_max_configs:
            supplied["max_configs"] = None
        try:
            async with db.async_session() as session, session.begin():
                server = await session.scalar(
                    select(Server).where(Server.id == server_id).with_for_update()
                )
                if server is None:
                    raise ServerNotFoundError(f"Server {server_id} not found")
                self._check_version(server, expected_version)
                endpoint_fields = {"ip", "port", "api_key"}
                endpoint_changed = any(
                    field in supplied and supplied[field] != getattr(server, field)
                    for field in endpoint_fields
                )
                if endpoint_changed and await self._managed_config_count(
                    session, server.id
                ):
                    raise InvalidOperationError(
                        "Drain all VPN configs before changing the Manager endpoint"
                    )
                if endpoint_changed:
                    # A new control-plane endpoint is a new trust boundary. It
                    # must be observed, have its identity learned, and be
                    # explicitly activated before placement can resume.
                    supplied["lifecycle_state"] = ServerLifecycleState.DISABLED.value
                    supplied["accepts_new_configs"] = False
                    supplied["manager_instance_id"] = None
                final_max = supplied.get("max_configs", server.max_configs)
                final_reserve = supplied.get(
                    "capacity_reserve", server.capacity_reserve
                )
                self._validate_capacity(final_max, final_reserve)
                for field, value in supplied.items():
                    setattr(server, field, value)
                server.version += 1
                changed = sorted(supplied)
                add_audit_event(
                    session,
                    request,
                    action="server.updated",
                    actor_user_id=principal.user_id,
                    target_type="server",
                    target_id=server.id,
                    details={
                        "changed_fields": changed,
                        "api_key_updated": "api_key" in supplied,
                        "endpoint_quarantined": endpoint_changed,
                        "previous_version": expected_version,
                        "new_version": server.version,
                    },
                )
        except (ServerNotFoundError, InvalidOperationError) as exc:
            await self._record_rejected_mutation(
                request=request,
                principal=principal,
                server_id=server_id,
                action="server.update.rejected",
                details={
                    "error_code": self._rejection_code(exc),
                    "expected_version": expected_version,
                    "changed_fields": sorted(supplied),
                    "api_key_updated": "api_key" in supplied,
                },
            )
            raise
        async with db.async_session() as session:
            persisted = await session.get(Server, server_id)
            assert persisted is not None
            return await self._server_payload(session, persisted, include_status=True)

    async def execute_action(
        self,
        server_id: int,
        *,
        request: Request,
        principal: AdminPrincipal,
        client_key: str,
        command: AdminServerActionRequest,
    ) -> dict[str, Any]:
        canonical = self._REMOTE_ACTIONS.get(command.action, command.action)
        if canonical in self._MUTATING_ACTIONS:
            try:
                return await self._execute_local_action(
                    server_id,
                    request=request,
                    principal=principal,
                    client_key=client_key,
                    canonical=canonical,
                    command=command,
                )
            except (ServerNotFoundError, InvalidOperationError) as exc:
                await self._record_action_rejection(
                    server_id=server_id,
                    request=request,
                    principal=principal,
                    client_key=client_key,
                    canonical=canonical,
                    command=command,
                    exc=exc,
                )
                raise
        return await self._execute_remote_action(
            server_id,
            request=request,
            principal=principal,
            client_key=client_key,
            canonical=canonical,
            command=command,
        )

    async def _execute_local_action(
        self,
        server_id: int,
        *,
        request: Request,
        principal: AdminPrincipal,
        client_key: str,
        canonical: str,
        command: AdminServerActionRequest,
    ) -> dict[str, Any]:
        if command.expected_version is None:
            raise FleetOptimisticConflict(
                "expected_version is required for server mutations"
            )
        payload = self._command_payload(command)
        async with db.async_session() as session, session.begin():
            server = await session.scalar(
                select(Server).where(Server.id == server_id).with_for_update()
            )
            if server is None:
                raise ServerNotFoundError(f"Server {server_id} not found")
            action, replayed = await self._stage_action(
                session,
                server=server,
                principal=principal,
                client_key=client_key,
                kind=canonical,
                expected_version=command.expected_version,
                reason=command.reason,
                payload=payload,
            )
            if replayed:
                return await self._action_payload(session, action, replayed=True)
            self._check_version(server, command.expected_version)
            previous_version = server.version
            managed_configs = await self._managed_config_count(session, server.id)
            if canonical in {"activate", "enable_new_configs"} or (
                canonical == "set_accepting" and command.accepts_new_configs is True
            ):
                await self._require_activation_readiness(session, server)
            self._apply_server_action(
                server, canonical, command, managed_configs=managed_configs
            )
            server.version += 1
            action.status = AdminActionStatus.SUCCEEDED.value
            action.result = {
                "server_id": server.id,
                "previous_version": previous_version,
                "version": server.version,
                "lifecycle_state": server.lifecycle_state,
                "accepts_new_configs": server.accepts_new_configs,
                "max_configs": server.max_configs,
                "capacity_reserve": server.capacity_reserve,
                "placement_weight": _decimal_string(server.placement_weight),
            }
            action.completed_at = _utcnow()
            add_audit_event(
                session,
                request,
                action=f"server.{canonical}",
                actor_user_id=principal.user_id,
                target_type="server",
                target_id=server.id,
                details={
                    "admin_action_id": action.action_id,
                    "reason": command.reason,
                    **action.result,
                },
            )
            await session.flush()
            return await self._action_payload(session, action)

    async def _execute_remote_action(
        self,
        server_id: int,
        *,
        request: Request,
        principal: AdminPrincipal,
        client_key: str,
        canonical: str,
        command: AdminServerActionRequest,
    ) -> dict[str, Any]:
        payload = self._command_payload(command)
        try:
            async with db.async_session() as session, session.begin():
                server = await session.scalar(
                    select(Server).where(Server.id == server_id).with_for_update()
                )
                if server is None:
                    raise ServerNotFoundError(f"Server {server_id} not found")
                action, replayed = await self._stage_action(
                    session,
                    server=server,
                    principal=principal,
                    client_key=client_key,
                    kind=canonical,
                    expected_version=command.expected_version,
                    reason=command.reason,
                    payload=payload,
                )
                if replayed:
                    return await self._action_payload(session, action, replayed=True)
                if command.expected_version is not None:
                    self._check_version(server, command.expected_version)
                target = _ManagerTarget(
                    id=server.id,
                    ip=server.ip,
                    port=server.port,
                    api_key=server.api_key,
                    manager_instance_id=server.manager_instance_id,
                )
                action_id = action.action_id
        except (ServerNotFoundError, InvalidOperationError) as exc:
            await self._record_action_rejection(
                server_id=server_id,
                request=request,
                principal=principal,
                client_key=client_key,
                canonical=canonical,
                command=command,
                exc=exc,
            )
            raise

        try:
            if canonical == "refresh_status":
                result = await self._refresh_status(target)
            elif canonical == "refresh_inventory":
                result = await self._refresh_inventory(target)
            elif canonical == "audit_drift":
                result = await self._audit_drift(target.id)
            else:  # pragma: no cover - schema and mapping invariant
                raise InvalidOperationError("Unsupported server action")
        except APIGatewayError as exc:
            await self._finish_remote_failure(
                target=target,
                action_id=action_id,
                request=request,
                principal=principal,
                error_code=type(exc).__name__,
            )
            raise AdminFleetRemoteError("VPN Manager request failed") from exc
        except InvalidOperationError as exc:
            await self._finish_remote_failure(
                target=target,
                action_id=action_id,
                request=request,
                principal=principal,
                error_code=type(exc).__name__,
            )
            raise
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish_remote_failure(
                    target=target,
                    action_id=action_id,
                    request=request,
                    principal=principal,
                    error_code="action_cancelled",
                )
            )
            raise
        except Exception as exc:
            await self._finish_remote_failure(
                target=target,
                action_id=action_id,
                request=request,
                principal=principal,
                error_code=type(exc).__name__,
            )
            raise

        async with db.async_session() as session, session.begin():
            action = await session.scalar(
                select(AdminAction)
                .where(AdminAction.action_id == action_id)
                .with_for_update()
            )
            if action is None:  # pragma: no cover - durable invariant
                raise RuntimeError("Durable admin action disappeared")
            server = await session.get(Server, target.id)
            if server is None:  # pragma: no cover - FK/lifecycle invariant
                raise ServerNotFoundError(f"Server {target.id} not found")
            status_record = None
            instance_mismatch = False
            if canonical in {"refresh_status", "refresh_inventory"}:
                status_record = VPNServerStatus(
                    server_id=target.id,
                    kind="status" if canonical == "refresh_status" else "inventory",
                    success=True,
                    snapshot=result["snapshot"],
                    manager_observed_at=result.get("manager_observed_at"),
                    manager_version=result.get("manager_version"),
                    manager_instance_id=result.get("manager_instance_id"),
                    inventory_revision=result.get("inventory_revision"),
                )
                session.add(status_record)
                await session.flush()
                await self._prune_status_history(session, target.id)
            observed_instance = result.get("manager_instance_id")
            if observed_instance:
                if server.manager_instance_id is None:
                    server.manager_instance_id = observed_instance
                elif server.manager_instance_id != observed_instance:
                    instance_mismatch = True
            action.status = AdminActionStatus.SUCCEEDED.value
            action.result = {
                **result["result"],
                "status_record_id": status_record.id if status_record else None,
                "instance_mismatch": instance_mismatch,
            }
            action.completed_at = _utcnow()
            add_audit_event(
                session,
                request,
                action=f"server.{canonical}",
                actor_user_id=principal.user_id,
                target_type="server",
                target_id=target.id,
                details={
                    "admin_action_id": action.action_id,
                    "reason": command.reason,
                    "result": action.result,
                },
            )
            return await self._action_payload(session, action)

    async def _refresh_status(self, target: _ManagerTarget) -> dict[str, Any]:
        async with self._gateway_factory(
            target.ip, target.port, target.api_key
        ) as gateway:
            manager_status = await gateway.get_status()
        snapshot = _jsonable(manager_status)
        return {
            "snapshot": snapshot,
            "manager_observed_at": manager_status.observed_at,
            "manager_version": manager_status.manager_version,
            "manager_instance_id": manager_status.instance_id,
            "inventory_revision": manager_status.inventory.revision,
            "result": {
                "manager_version": manager_status.manager_version,
                "manager_instance_id": manager_status.instance_id,
                "manager_ready": manager_status.readiness.ready,
                "data_plane_status": manager_status.data_plane.status,
                "online_sessions": manager_status.data_plane.online_sessions,
                "inventory_revision": manager_status.inventory.revision,
            },
        }

    async def _refresh_inventory(self, target: _ManagerTarget) -> dict[str, Any]:
        async with self._gateway_factory(
            target.ip, target.port, target.api_key
        ) as gateway:
            inventory = await gateway.get_client_inventory()
        if inventory is None:  # pragma: no cover - unconditional request invariant
            raise InvalidOperationError("Manager inventory was unexpectedly unchanged")
        states = Counter(client.state for client in inventory.clients)
        counts = {
            state: states[state]
            for state in (
                "active",
                "suspended",
                "revoked",
                "expired",
                "incomplete",
                "orphaned",
                "unknown",
            )
        }
        snapshot = {
            "availability": "available",
            "revision": inventory.revision,
            "count": inventory.count,
            "counts": {"total": inventory.count, **counts},
        }
        return {
            "snapshot": snapshot,
            "inventory_revision": inventory.revision,
            "result": snapshot,
        }

    async def _audit_drift(self, server_id: int) -> dict[str, Any]:
        report = await self._drift_service.audit_server(server_id)
        findings = [
            {
                "config_id": item.config_id,
                "name": item.name,
                "code": item.reason,
                "reason": item.reason,
                "severity": item.severity,
                "desired_state": item.desired_state,
                "hub_actual_state": item.hub_actual_state,
                "manager_state": item.manager_state,
                "repairable": item.repairable,
                "details": list(item.details),
                "message": item.reason.replace("_", " "),
            }
            for item in report.findings
        ]
        result = {
            "server_id": report.server_id,
            "inventory_revision": report.inventory_revision,
            "inventory_etag": report.inventory_etag,
            "unchanged": report.unchanged,
            "findings": findings,
            "finding_count": len(findings),
        }
        return {"result": result}

    async def _finish_remote_failure(
        self,
        *,
        target: _ManagerTarget,
        action_id: str,
        request: Request,
        principal: AdminPrincipal,
        error_code: str,
    ) -> None:
        async with db.async_session() as session, session.begin():
            action = await session.scalar(
                select(AdminAction)
                .where(AdminAction.action_id == action_id)
                .with_for_update()
            )
            if action is None:
                return
            if action.kind in {"refresh_status", "refresh_inventory"}:
                failed_status = VPNServerStatus(
                    server_id=target.id,
                    kind=("status" if action.kind == "refresh_status" else "inventory"),
                    success=False,
                    snapshot={},
                    error_code=error_code[:96],
                )
                session.add(failed_status)
                await session.flush()
                await self._prune_status_history(session, target.id)
            action.status = AdminActionStatus.FAILED.value
            action.error_code = error_code[:96]
            action.error_detail = "VPN Manager request failed"
            action.completed_at = _utcnow()
            add_audit_event(
                session,
                request,
                action=f"server.{action.kind}.failed",
                actor_user_id=principal.user_id,
                target_type="server",
                target_id=target.id,
                details={
                    "admin_action_id": action.action_id,
                    "error_code": action.error_code,
                },
            )

    @staticmethod
    def _rejection_code(exc: Exception) -> str:
        if isinstance(exc, FleetIdempotencyConflict):
            return "idempotency_conflict"
        if isinstance(exc, FleetOptimisticConflict):
            return "optimistic_conflict"
        if isinstance(exc, ServerNotFoundError):
            return "server_not_found"
        return "invalid_operation"

    async def _record_rejected_mutation(
        self,
        *,
        request: Request,
        principal: AdminPrincipal,
        server_id: int,
        action: str,
        details: dict[str, Any],
        discard_running_action: tuple[str, str] | None = None,
    ) -> None:
        """Persist a safe rejection event after the failed transaction rolled back."""

        async with db.async_session() as session, session.begin():
            if discard_running_action is not None:
                key_hash, request_hash = discard_running_action
                # PostgreSQL rolls the staged row back with the rejected outer
                # transaction. The explicit, tightly-fenced cleanup also keeps
                # SQLite/test savepoint behavior from leaving a ghost RUNNING
                # action and is harmless when no row survived.
                await session.execute(
                    delete(AdminAction).where(
                        AdminAction.actor_user_id == principal.user_id,
                        AdminAction.idempotency_key_hash == key_hash,
                        AdminAction.request_hash == request_hash,
                        AdminAction.status == AdminActionStatus.RUNNING.value,
                    )
                )
            add_audit_event(
                session,
                request,
                action=action,
                actor_user_id=principal.user_id,
                target_type="server",
                target_id=server_id,
                details={"outcome": "rejected", **_jsonable(details)},
            )

    async def _record_action_rejection(
        self,
        *,
        server_id: int,
        request: Request,
        principal: AdminPrincipal,
        client_key: str,
        canonical: str,
        command: AdminServerActionRequest,
        exc: Exception,
    ) -> None:
        payload = self._command_payload(command)
        key_hash = hashlib.sha256(client_key.strip().encode("utf-8")).hexdigest()
        request_hash = self._action_request_hash(
            server_id=server_id,
            kind=canonical,
            expected_version=command.expected_version,
            reason=command.reason,
            payload=payload,
        )
        await self._record_rejected_mutation(
            request=request,
            principal=principal,
            server_id=server_id,
            action=f"server.{canonical}.rejected",
            details={
                "error_code": self._rejection_code(exc),
                "requested_action": canonical,
                "reason": command.reason,
                "expected_version": command.expected_version,
                "idempotency_key_hash": key_hash,
                "request_hash": request_hash,
            },
            discard_running_action=(key_hash, request_hash),
        )

    @staticmethod
    def _action_request_hash(
        *,
        server_id: int,
        kind: str,
        expected_version: int | None,
        reason: str,
        payload: dict[str, Any],
    ) -> str:
        request_payload = {
            "server_id": server_id,
            "kind": kind,
            "expected_version": expected_version,
            "reason": reason,
            "payload": payload,
        }
        return hashlib.sha256(
            json.dumps(
                request_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode()
        ).hexdigest()

    async def _stage_action(
        self,
        session,
        *,
        server: Server,
        principal: AdminPrincipal,
        client_key: str,
        kind: str,
        expected_version: int | None,
        reason: str,
        payload: dict[str, Any],
    ) -> tuple[AdminAction, bool]:
        normalized_key = client_key.strip()
        if not normalized_key:
            raise InvalidOperationError("Idempotency-Key must not be blank")
        key_hash = hashlib.sha256(normalized_key.encode()).hexdigest()
        request_hash = self._action_request_hash(
            server_id=server.id,
            kind=kind,
            expected_version=expected_version,
            reason=reason,
            payload=payload,
        )
        existing = await session.scalar(
            select(AdminAction).where(
                AdminAction.actor_user_id == principal.user_id,
                AdminAction.idempotency_key_hash == key_hash,
            )
        )
        if existing is not None:
            if existing.request_hash != request_hash:
                raise FleetIdempotencyConflict(
                    "Idempotency-Key was already used for another fleet action"
                )
            return existing, True
        action = AdminAction(
            server_id=server.id,
            actor_user_id=principal.user_id,
            kind=kind,
            status=AdminActionStatus.RUNNING.value,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
            expected_server_version=expected_version,
            reason=reason,
            payload=payload,
            result={},
            started_at=_utcnow(),
        )
        try:
            async with session.begin_nested():
                session.add(action)
                await session.flush()
            return action, False
        except IntegrityError:
            existing = await session.scalar(
                select(AdminAction).where(
                    AdminAction.actor_user_id == principal.user_id,
                    AdminAction.idempotency_key_hash == key_hash,
                )
            )
            if existing is None:  # pragma: no cover - unique constraint invariant
                raise
            if existing.request_hash != request_hash:
                raise FleetIdempotencyConflict(
                    "Idempotency-Key was already used for another fleet action"
                )
            return existing, True

    @staticmethod
    def _command_payload(command: AdminServerActionRequest) -> dict[str, Any]:
        payload = command.model_dump(
            exclude={"action", "reason", "expected_version"}, exclude_none=True
        )
        return _jsonable(payload)

    def _apply_server_action(
        self,
        server: Server,
        action: str,
        command: AdminServerActionRequest,
        *,
        managed_configs: int,
    ) -> None:
        if action == "set_accepting":
            if command.accepts_new_configs is None:
                raise InvalidOperationError("accepts_new_configs is required")
            if command.accepts_new_configs and server.lifecycle_state != "active":
                raise InvalidOperationError(
                    "Only active servers may accept new configurations"
                )
            server.accepts_new_configs = command.accepts_new_configs
        elif action == "enable_new_configs":
            if server.lifecycle_state != "active":
                raise InvalidOperationError(
                    "Only active servers may accept new configurations"
                )
            server.accepts_new_configs = True
        elif action == "disable_new_configs":
            server.accepts_new_configs = False
        elif action in {"drain", "start_drain"}:
            if server.lifecycle_state == ServerLifecycleState.RETIRED.value:
                raise InvalidOperationError("A retired server cannot be drained")
            server.lifecycle_state = ServerLifecycleState.DRAINING.value
            server.accepts_new_configs = False
        elif action == "disable":
            if server.lifecycle_state == ServerLifecycleState.RETIRED.value:
                raise InvalidOperationError("A retired server cannot be disabled")
            server.lifecycle_state = ServerLifecycleState.DISABLED.value
            server.accepts_new_configs = False
        elif action == "activate":
            if server.lifecycle_state == ServerLifecycleState.RETIRED.value:
                raise InvalidOperationError("A retired server cannot be reactivated")
            server.lifecycle_state = ServerLifecycleState.ACTIVE.value
            server.accepts_new_configs = True
        elif action == "retire":
            if managed_configs:
                raise InvalidOperationError(
                    "A server with managed configs cannot be retired"
                )
            server.lifecycle_state = ServerLifecycleState.RETIRED.value
            server.accepts_new_configs = False
        elif action == "update_capacity":
            requested_max = command.max_configs or command.capacity
            if command.clear_max_configs:
                requested_max = None
            if requested_max is None and not command.clear_max_configs:
                raise InvalidOperationError("capacity is required")
            reserve = (
                command.capacity_reserve
                if command.capacity_reserve is not None
                else server.capacity_reserve
            )
            self._validate_capacity(requested_max, reserve)
            server.max_configs = requested_max
            server.capacity_reserve = reserve
            if command.placement_weight is not None:
                server.placement_weight = command.placement_weight
        else:  # pragma: no cover - schema invariant
            raise InvalidOperationError("Unsupported server action")

    @staticmethod
    def _check_version(server: Server, expected_version: int) -> None:
        if server.version != expected_version:
            raise FleetOptimisticConflict(
                f"Server changed: expected version {expected_version}, current {server.version}"
            )

    @staticmethod
    def _validate_capacity(max_configs: int | None, reserve: int) -> None:
        if max_configs is not None and reserve >= max_configs:
            raise InvalidOperationError("capacity_reserve must be below max_configs")

    async def _require_activation_readiness(self, session, server: Server) -> None:
        latest = await self._latest_status(session, server.id, kind="status")
        decision = manager_readiness_decision(server, latest, now=_utcnow())
        if decision.reason == "health_check_required":
            raise InvalidOperationError(
                "A fresh successful Manager health check is required before activation"
            )
        if decision.reason == "manager_identity_mismatch":
            raise InvalidOperationError(
                "Manager instance identity must match before activation"
            )
        if not decision.allowed:
            raise InvalidOperationError(
                "Manager and OpenVPN data plane must be healthy before activation"
            )

    @staticmethod
    async def _managed_config_count(session, server_id: int) -> int:
        value = await session.scalar(
            select(func.count(VPN_Config.id)).where(
                VPN_Config.server_id == server_id,
                managed_config_condition(),
            )
        )
        return int(value or 0)

    @staticmethod
    async def _prune_status_history(session, server_id: int) -> None:
        stale_ids = (
            select(VPNServerStatus.id)
            .where(VPNServerStatus.server_id == server_id)
            .order_by(VPNServerStatus.collected_at.desc(), VPNServerStatus.id.desc())
            .offset(settings.admin_fleet_status_retention_per_server)
        )
        await session.execute(
            delete(VPNServerStatus).where(VPNServerStatus.id.in_(stale_ids))
        )

    async def _config_counts(self, session, server_id: int) -> dict[str, int]:
        rows = (
            await session.scalars(
                select(VPN_Config).where(
                    VPN_Config.server_id == server_id,
                    managed_config_condition(),
                )
            )
        ).all()
        return {
            "configs_count": len(rows),
            "active_configs": sum(
                row.actual_state == VPNState.ACTIVE.value for row in rows
            ),
            "suspended_configs": sum(
                row.actual_state == VPNState.SUSPENDED.value for row in rows
            ),
            "pending_configs": sum(
                row.desired_state != row.actual_state for row in rows
            ),
        }

    async def _server_payload(
        self, session, server: Server, *, include_status: bool = False
    ) -> dict[str, Any]:
        counts = await self._config_counts(session, server.id)
        latest = await self._latest_status(session, server.id, kind="status")
        latest_inventory = await self._latest_status(
            session, server.id, kind="inventory"
        )
        latest_seen = max(
            (item for item in (latest, latest_inventory) if item is not None),
            key=lambda item: (item.collected_at, item.id),
            default=None,
        )
        health = self._health(server, latest)
        available_capacity = (
            None
            if server.max_configs is None
            else max(
                0,
                server.max_configs - server.capacity_reserve - counts["configs_count"],
            )
        )
        payload = {
            "id": server.id,
            "name": server.name,
            "ip": server.ip,
            "port": server.port,
            "host": server.host,
            "location": server.location,
            "provider": server.provider,
            "public_endpoint": server.public_endpoint,
            "vpn_endpoint": server.public_endpoint or server.host,
            "monthly_cost": _money(server.monthly_cost),
            "lifecycle_state": server.lifecycle_state,
            "status": server.lifecycle_state,
            "health": health,
            "accepts_new_configs": server.accepts_new_configs,
            "max_configs": server.max_configs,
            "capacity": server.max_configs,
            "capacity_reserve": server.capacity_reserve,
            "available_capacity": available_capacity,
            "placement_weight": _decimal_string(server.placement_weight),
            "manager_instance_id": server.manager_instance_id,
            "version": server.version,
            "updated_at": _jsonable(server.updated_at),
            "api_key_configured": bool(server.api_key),
            "last_seen_at": (
                _jsonable(latest_seen.collected_at) if latest_seen else None
            ),
            "inventory_revision": (
                latest_inventory.inventory_revision
                if latest_inventory
                else latest.inventory_revision if latest else None
            ),
            **counts,
        }
        if include_status:
            payload["latest_status"] = (
                await self._status_payload(session, server, latest) if latest else None
            )
        return payload

    async def _latest_status(self, session, server_id: int, *, kind: str | None = None):
        conditions = [VPNServerStatus.server_id == server_id]
        if kind is not None:
            conditions.append(VPNServerStatus.kind == kind)
        return await session.scalar(
            select(VPNServerStatus)
            .where(*conditions)
            .order_by(VPNServerStatus.collected_at.desc(), VPNServerStatus.id.desc())
            .limit(1)
        )

    async def _status_payload(
        self, session, server: Server, latest: VPNServerStatus | None
    ) -> dict[str, Any]:
        counts = await self._config_counts(session, server.id)
        latest_inventory = await self._latest_status(
            session, server.id, kind="inventory"
        )
        snapshot = latest.snapshot if latest and latest.success else {}
        readiness = snapshot.get("readiness", {}) if isinstance(snapshot, dict) else {}
        data_plane = (
            snapshot.get("data_plane", {}) if isinstance(snapshot, dict) else {}
        )
        inventory = (
            snapshot.get("inventory", snapshot) if isinstance(snapshot, dict) else {}
        )
        pki = snapshot.get("pki", {}) if isinstance(snapshot, dict) else {}
        certificate = pki.get("server_certificate", {}) if isinstance(pki, dict) else {}
        last_drift = await session.scalar(
            select(AdminAction)
            .where(
                AdminAction.server_id == server.id,
                AdminAction.kind == "audit_drift",
                AdminAction.status == AdminActionStatus.SUCCEEDED.value,
            )
            .order_by(AdminAction.completed_at.desc(), AdminAction.id.desc())
            .limit(1)
        )
        drift_result = last_drift.result if last_drift else {}
        drift = (
            drift_result.get("findings", []) if isinstance(drift_result, dict) else []
        )
        return {
            "server_id": server.id,
            "status": self._health(server, latest),
            "reachable": bool(latest and latest.success),
            "manager_ready": readiness.get("ready", False),
            "openvpn_ready": data_plane.get("status") == "up",
            "online_sessions": data_plane.get("online_sessions"),
            "bytes_received": data_plane.get("bytes_received"),
            "bytes_sent": data_plane.get("bytes_sent"),
            "active_configs": counts["active_configs"],
            "configs_count": counts["configs_count"],
            "capacity": server.max_configs,
            "certificate_expires_at": certificate.get("expires_at"),
            "last_checked_at": _jsonable(latest.collected_at) if latest else None,
            "inventory_revision": (
                latest_inventory.inventory_revision
                if latest_inventory
                else latest.inventory_revision if latest else inventory.get("revision")
            ),
            "manager_version": latest.manager_version if latest else None,
            "manager_instance_id": latest.manager_instance_id if latest else None,
            "instance_mismatch": bool(
                latest
                and latest.manager_instance_id
                and server.manager_instance_id
                and latest.manager_instance_id != server.manager_instance_id
            ),
            "error_code": latest.error_code if latest else None,
            "drift": drift,
            "snapshot": snapshot,
        }

    @staticmethod
    def _health(server: Server, latest: VPNServerStatus | None) -> str:
        if server.lifecycle_state in {
            ServerLifecycleState.DISABLED.value,
            ServerLifecycleState.RETIRED.value,
        }:
            return server.lifecycle_state
        if latest is None:
            return "unknown"
        if not latest.success:
            return "unreachable"
        collected_at = latest.collected_at
        if collected_at.tzinfo is None:
            collected_at = collected_at.replace(tzinfo=timezone.utc)
        if (
            _utcnow() - collected_at.astimezone(timezone.utc)
        ).total_seconds() > settings.admin_fleet_status_stale_seconds:
            return "stale"
        if (
            latest.manager_instance_id
            and server.manager_instance_id
            and latest.manager_instance_id != server.manager_instance_id
        ):
            return "instance_mismatch"
        snapshot = latest.snapshot or {}
        readiness = snapshot.get("readiness", {})
        plane = snapshot.get("data_plane", {})
        if readiness and not readiness.get("ready", False):
            return "unhealthy"
        if plane and plane.get("status") != "up":
            return "unhealthy"
        return "healthy"

    @staticmethod
    def _status_record(row: VPNServerStatus) -> dict[str, Any]:
        return {
            "id": row.id,
            "server_id": row.server_id,
            "kind": row.kind,
            "success": row.success,
            "snapshot": row.snapshot,
            "error_code": row.error_code,
            "manager_observed_at": _jsonable(row.manager_observed_at),
            "manager_version": row.manager_version,
            "manager_instance_id": row.manager_instance_id,
            "inventory_revision": row.inventory_revision,
            "collected_at": _jsonable(row.collected_at),
        }

    @staticmethod
    async def _action_payload(
        session, action: AdminAction, *, replayed: bool = False
    ) -> dict[str, Any]:
        actor = await session.get(AdminUser, action.actor_user_id)
        return {
            "id": action.action_id,
            "action_id": action.action_id,
            "server_id": action.server_id,
            "type": action.kind,
            "kind": action.kind,
            "status": action.status,
            "requested_by": actor.username if actor else None,
            "actor_user_id": action.actor_user_id,
            "reason": action.reason,
            "expected_server_version": action.expected_server_version,
            "result": action.result,
            "error": action.error_detail,
            "error_code": action.error_code,
            "created_at": _jsonable(action.created_at),
            "updated_at": _jsonable(action.completed_at or action.started_at),
            "started_at": _jsonable(action.started_at),
            "completed_at": _jsonable(action.completed_at),
            "replayed": replayed,
        }
