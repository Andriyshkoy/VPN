"""Fixtures for purchases committed before new OpenVPN sales were retired.

Tests seed the historical atomic intent directly so financial replay, rollback
and compensation regressions remain covered without reopening the public sale
path. New product issuance never uses this helper.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid4, uuid5

from core.db.models.ledger import LedgerKind
from core.db.repo.billing import to_money
from core.exceptions import APIConfigurationError, APIRequestRejectedError


async def prepare_historical_paid_config(
    billing,
    *,
    server_id,
    owner_id,
    name,
    display_name,
    creation_cost,
    use_password=False,
    idempotency_key=None,
):
    """Commit the old intent and debit together without making a remote call."""

    amount = to_money(creation_cost)
    operation_id = (
        str(uuid4())
        if idempotency_key is None
        else str(uuid5(NAMESPACE_URL, f"vpn-paid-config:{owner_id}:{idempotency_key}"))
    )
    async with billing._uow() as repos:
        await repos["users"].get_for_update(owner_id)
        context = await billing._config_service.prepare_config(
            repos=repos,
            operation_id=operation_id,
            server_id=server_id,
            owner_id=owner_id,
            name=name,
            display_name=display_name,
            use_password=use_password,
            operation_payload={
                "paid_request_version": 1,
                "creation_cost": f"{amount:.2f}",
                "display_name": display_name,
            },
        )
        if amount > 0:
            await repos["billing"].apply_balance_change(
                user_id=owner_id,
                amount=-amount,
                kind=LedgerKind.CONFIG_RESERVATION,
                idempotency_key=f"config-reservation:{operation_id}",
                allow_negative_balance=False,
                reference_type="vpn_operation",
                reference_id=operation_id,
                details={"server_id": server_id, "config_name": name},
            )
    return context


async def create_historical_paid_config(billing, **kwargs):
    """Execute a pre-existing historical purchase and its original compensation."""

    context = await prepare_historical_paid_config(billing, **kwargs)
    try:
        return await billing._config_service.execute_prepared(context)
    except (APIConfigurationError, APIRequestRejectedError):
        async with billing._uow() as repos:
            await repos["billing"].refund_rejected_config_reservations(
                operation_id=context.operation_id
            )
        raise
