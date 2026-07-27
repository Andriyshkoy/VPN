"""Converge durable suspension intents without touching billing or the ledger."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import select

from core.db.models import Server, VPN_Config, VPNOperation
from core.db.unit_of_work import uow
from core.domain import VPNOperationKind, VPNOperationStatus, VPNState
from core.services import ConfigService

_RETRYABLE_STATUSES = {
    VPNOperationStatus.PENDING.value,
    VPNOperationStatus.RUNNING.value,
    VPNOperationStatus.FAILED.value,
}


@dataclass(frozen=True, slots=True)
class SuspensionTarget:
    config_id: int
    operation_id: str


async def _load_targets() -> tuple[SuspensionTarget, ...]:
    async with uow() as repos:
        session = repos["configs"].session
        configs = (
            await session.execute(
                select(
                    VPN_Config.id,
                    VPN_Config.name,
                    VPN_Config.server_id,
                    VPN_Config.owner_id,
                    Server.manager_instance_id,
                    VPN_Config.operation_id,
                )
                .join(Server, Server.id == VPN_Config.server_id)
                .where(
                    VPN_Config.desired_state == VPNState.SUSPENDED.value,
                    VPN_Config.actual_state.is_distinct_from(VPNState.SUSPENDED.value),
                )
                .order_by(VPN_Config.id)
            )
        ).all()
        if not configs:
            return ()
        if any(operation_id is None for *_, operation_id in configs):
            raise RuntimeError("Suspension drift is missing a durable operation")
        if any(not manager_instance_id for *_, manager_instance_id, _ in configs):
            raise RuntimeError(
                "Suspension drift targets a Manager without pinned identity"
            )

        operation_ids = tuple(operation_id for *_, operation_id in configs)
        operations = (
            await session.scalars(
                select(VPNOperation).where(VPNOperation.operation_id.in_(operation_ids))
            )
        ).all()
        operations_by_id = {
            operation.operation_id: operation for operation in operations
        }

        targets = []
        for (
            config_id,
            config_name,
            server_id,
            owner_id,
            _manager_instance_id,
            operation_id,
        ) in configs:
            operation = operations_by_id.get(operation_id)
            if operation is None:
                raise RuntimeError("Suspension drift operation is missing")
            if (
                operation.config_id != config_id
                or operation.kind != VPNOperationKind.SUSPEND.value
                or operation.status not in _RETRYABLE_STATUSES
            ):
                raise RuntimeError("Suspension drift operation is not safely retryable")
            if (
                operation.config_name != config_name
                or operation.server_id != server_id
                or operation.owner_id != owner_id
            ):
                raise RuntimeError(
                    "Suspension drift operation snapshot does not match its config"
                )
            targets.append(
                SuspensionTarget(
                    config_id=config_id,
                    operation_id=operation_id,
                )
            )
        return tuple(targets)


async def _assert_succeeded(operation_ids: tuple[str, ...]) -> None:
    if not operation_ids:
        return
    async with uow() as repos:
        operations = await repos["vpn_operations"].list_by_operation_ids(operation_ids)
    if len(operations) != len(operation_ids) or any(
        operation.status != VPNOperationStatus.SUCCEEDED.value
        or operation.lease_token is not None
        or operation.lease_until is not None
        or operation.completed_at is None
        for operation in operations
    ):
        raise RuntimeError("Suspension operations did not converge cleanly")


async def reconcile_suspended_configs(*, timeout_seconds: float = 300) -> int:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
    ):
        raise ValueError("Suspension reconciliation timeout must be positive")

    service = ConfigService(uow)
    deadline = time.monotonic() + float(timeout_seconds)
    pending_count = 0

    def remaining_budget() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    try:
        initial = await asyncio.wait_for(
            _load_targets(),
            timeout=remaining_budget(),
        )
        pending = initial
        pending_count = len(pending)
        operation_ids = tuple(target.operation_id for target in initial)

        while pending:
            await asyncio.wait_for(
                service.execute_operations([target.operation_id for target in pending]),
                timeout=remaining_budget(),
            )
            pending = await asyncio.wait_for(
                _load_targets(),
                timeout=remaining_budget(),
            )
            pending_count = len(pending)
            if pending:
                await asyncio.sleep(min(2, remaining_budget()))

        await asyncio.wait_for(
            _assert_succeeded(operation_ids),
            timeout=remaining_budget(),
        )
    except TimeoutError as exc:
        raise RuntimeError(
            f"Suspension reconciliation timed out with {pending_count} pending"
        ) from exc

    return len(initial)


if __name__ == "__main__":
    reconciled = asyncio.run(reconcile_suspended_configs())
    print(f"release suspension reconciliation completed: {reconciled}")
