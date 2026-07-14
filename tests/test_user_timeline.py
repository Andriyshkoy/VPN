from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from admin.routers.admin_v1_users import router as users_router
from admin.security import AdminPrincipal, get_admin_principal
from core.config import settings
from core.db.models import (
    AdminAuditEvent,
    AdminRole,
    AdminUser,
    LedgerEntry,
    ProviderPayment,
    ReferralReward,
    TelegramUpdateInbox,
    TelegramUserActionEvent,
    User,
    VPNOperation,
)
from core.db.repo.telegram_user_action import TelegramUserActionRepo
from core.db.unit_of_work import uow
from core.domain.telegram import TelegramUpdateStatus
from core.exceptions import InvalidOperationError
from core.services.telegram_updates import TelegramUpdateService
from core.services.telegram_user_actions import (
    TelegramActionAuditContext,
    classify_telegram_action,
)
from core.services.user import UserService
from core.services.user_timeline import AdminUserTimelineService

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _private_message(update_id: int, tg_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1,
            "chat": {"id": tg_id, "type": "private"},
            "from": {"id": tg_id, "is_bot": False, "first_name": "User"},
            "text": text,
        },
    }


def test_bot_taxonomy_never_copies_untrusted_content_or_target_ids():
    secret = "private config name and payment payload"
    message = classify_telegram_action(_private_message(1, 100, secret))
    assert message.action == "message.received"
    assert message.result == "handled"
    assert message.metadata == {"content_type": "text"}
    assert secret not in json.dumps(message.metadata)

    start = classify_telegram_action(
        _private_message(2, 100, "/start ref_super-secret-invite")
    )
    assert start.action == "navigation.start"
    assert start.metadata == {}

    forged = classify_telegram_action(
        {
            "update_id": 3,
            "callback_query": {
                "id": "callback-secret",
                "from": {"id": 100},
                "data": "del_ok:987654321",
                "message": {"chat": {"id": 100, "type": "private"}},
            },
        }
    )
    assert forged.action == "vpn.config_delete_confirm"
    assert forged.metadata == {}

    payment = classify_telegram_action(
        {
            "update_id": 4,
            "pre_checkout_query": {
                "id": "query-id",
                "from": {"id": 100},
                "invoice_payload": "topup:must-not-survive",
                "provider_payment_charge_id": "provider-secret",
            },
        }
    )
    assert payment.action == "finance.payment_pre_checkout"
    assert payment.metadata == {}

    group = classify_telegram_action(
        {
            **_private_message(5, 100, "/balance"),
            "message": {
                **_private_message(5, 100, "/balance")["message"],
                "chat": {"id": -100, "type": "group"},
            },
        }
    )
    assert group.action == "privacy.non_private_input"
    assert group.result == "ignored"


@pytest.mark.asyncio
async def test_fenced_ack_appends_exactly_one_safe_event(sessionmaker):
    user = await UserService(uow).register(810_001, username="timeline-user")
    service = TelegramUpdateService(uow)
    raw_secret = "my laptop config PRIVATE-CONTENT"
    await service.ingest(_private_message(81, user.tg_id, raw_secret))
    claimed = await service.claim_next(now=NOW)
    assert claimed is not None

    stale = replace(claimed, lease_token="not-the-owner")
    assert not await service.mark_processed(stale, now=NOW)
    async with sessionmaker() as session:
        assert await session.scalar(select(TelegramUserActionEvent.id)) is None

    context = TelegramActionAuditContext.from_payload(claimed.payload)
    context.record(
        "vpn.config_rename",
        metadata={
            "config_id": 17,
            "payload": raw_secret,
            "token": "bot-token",
        },
    )
    assert await service.mark_processed(claimed, now=NOW, audit_context=context)
    assert not await service.mark_processed(claimed, now=NOW, audit_context=context)

    async with sessionmaker() as session:
        events = (await session.scalars(select(TelegramUserActionEvent))).all()
        inbox = await session.scalar(select(TelegramUpdateInbox))
    assert len(events) == 1
    event = events[0]
    assert event.source_update_id == 81
    assert event.action == "vpn.config_rename"
    assert event.result == "completed"
    assert event.metadata_json == {"config_id": 17}
    assert event.occurred_at == claimed.received_at
    assert inbox.payload == {}
    assert raw_secret not in json.dumps(event.metadata_json)


@pytest.mark.asyncio
async def test_fenced_ack_and_audit_append_roll_back_as_one_transaction(
    sessionmaker, monkeypatch
):
    user = await UserService(uow).register(810_010, username="atomic-user")
    service = TelegramUpdateService(uow)
    await service.ingest(_private_message(810, user.tg_id, "/balance"))
    claimed = await service.claim_next(now=NOW)
    assert claimed is not None

    async def fail_append(*_args, **_kwargs):
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(TelegramUserActionRepo, "append_once", fail_append)
    with pytest.raises(RuntimeError, match="audit storage failure"):
        await service.mark_processed(claimed, now=NOW)

    async with sessionmaker() as session:
        inbox = await session.scalar(
            select(TelegramUpdateInbox).where(TelegramUpdateInbox.update_id == 810)
        )
        assert inbox.status == TelegramUpdateStatus.PROCESSING.value
        assert inbox.payload["message"]["text"] == "/balance"
        assert await session.scalar(select(TelegramUserActionEvent.id)) is None


@pytest.mark.asyncio
async def test_terminal_failures_are_safe_and_auto_dead_is_audited(
    sessionmaker, monkeypatch
):
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 1)
    user = await UserService(uow).register(810_002, username="failed-user")
    service = TelegramUpdateService(uow)

    await service.ingest(
        {
            "update_id": 82,
            "callback_query": {
                "id": "secret-callback",
                "from": {"id": user.tg_id},
                "data": "del_ok:999999",
                "message": {"chat": {"id": user.tg_id, "type": "private"}},
            },
        }
    )
    claimed = await service.claim_next(now=NOW)
    assert claimed is not None
    assert await service.mark_failed(
        claimed,
        RuntimeError("config body and token must not enter the audit"),
        now=NOW,
    )

    await service.ingest(_private_message(83, user.tg_id, "another secret"))
    crashed = await service.claim_next(now=NOW + timedelta(seconds=1))
    assert crashed is not None
    assert await service.claim_next(now=NOW + timedelta(seconds=302)) is None

    monkeypatch.setattr(settings, "telegram_update_max_attempts", 2)
    await service.ingest(_private_message(84, user.tg_id, "/help"))
    lowered = await service.claim_next(now=NOW + timedelta(seconds=400))
    assert lowered is not None
    assert await service.mark_failed(
        lowered,
        RuntimeError("first retry"),
        now=NOW + timedelta(seconds=400),
    )
    monkeypatch.setattr(settings, "telegram_update_max_attempts", 1)
    assert await service.claim_next(now=NOW + timedelta(seconds=402)) is None

    async with sessionmaker() as session:
        events = (
            await session.scalars(
                select(TelegramUserActionEvent).order_by(
                    TelegramUserActionEvent.source_update_id
                )
            )
        ).all()
    assert [(event.source_update_id, event.result) for event in events] == [
        (82, "failed"),
        (83, "failed"),
        (84, "failed"),
    ]
    assert events[0].metadata_json == {"attempts": 1, "error_type": "runtime"}
    assert events[1].metadata_json == {
        "attempts": 1,
        "error_type": "lease_expired",
    }
    assert events[2].metadata_json == {
        "attempts": 1,
        "error_type": "retry_budget_exhausted",
    }
    serialized = json.dumps([event.metadata_json for event in events])
    assert "token must not" not in serialized
    assert "999999" not in serialized


async def _seed_timeline(sessionmaker):
    async with sessionmaker() as session, session.begin():
        source = User(tg_id=820_001, username="source", balance=Decimal("100.00"))
        target = User(tg_id=820_002, username="target", balance=Decimal("25.00"))
        admin = AdminUser(
            username="ops",
            password_hash="$2b$12$not-used",
            role=AdminRole.OPS.value,
        )
        session.add_all([source, target, admin])
        await session.flush()

        target_ledger = LedgerEntry(
            user_id=target.id,
            amount=Decimal("20.00"),
            balance_after=Decimal("20.00"),
            kind="manual_top_up",
            idempotency_key="timeline-target-ledger",
            details={},
            created_at=NOW + timedelta(minutes=1),
        )
        source_ledger = LedgerEntry(
            user_id=source.id,
            amount=Decimal("100.00"),
            balance_after=Decimal("100.00"),
            kind="provider_payment",
            idempotency_key="timeline-source-payment-ledger",
            details={},
            created_at=NOW + timedelta(minutes=2),
        )
        reward_ledger = LedgerEntry(
            user_id=target.id,
            amount=Decimal("5.00"),
            balance_after=Decimal("25.00"),
            kind="referral_reward_l1",
            idempotency_key="timeline-reward-ledger",
            details={},
            created_at=NOW + timedelta(minutes=3),
        )
        session.add_all([target_ledger, source_ledger, reward_ledger])
        await session.flush()

        target_payment = ProviderPayment(
            intent_id="timeline-target-payment",
            user_id=target.id,
            provider="telegram",
            amount=Decimal("300.00"),
            currency="RUB",
            payload="topup:private-payload",
            raw_data={"token": "must-never-leak"},
            status="pending",
            created_at=NOW + timedelta(minutes=4),
            expires_at=NOW + timedelta(hours=1),
        )
        source_payment = ProviderPayment(
            intent_id="timeline-source-payment",
            user_id=source.id,
            provider="telegram",
            provider_payment_id="provider-private-id",
            amount=Decimal("100.00"),
            currency="RUB",
            payload="topup:source-private-payload",
            status="credited",
            ledger_entry_id=source_ledger.id,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=1),
            credited_at=NOW + timedelta(minutes=2),
            referral_settled_at=NOW + timedelta(minutes=3),
            referral_program_version="v1-5pct-1pct",
            referral_settlement_status="rewarded",
        )
        session.add_all([target_payment, source_payment])
        await session.flush()
        session.add(
            ReferralReward(
                source_payment_id=source_payment.id,
                source_user_id=source.id,
                beneficiary_user_id=target.id,
                level=1,
                rate_bps=500,
                source_amount=Decimal("100.00"),
                reward_amount=Decimal("5.00"),
                currency="RUB",
                ledger_entry_id=reward_ledger.id,
                created_at=NOW + timedelta(minutes=3),
            )
        )
        session.add(
            TelegramUserActionEvent(
                user_id=target.id,
                source_update_id=820,
                category="bot",
                action="finance.payment_amount_select",
                result="handled",
                metadata_json={
                    "amount_rub": 300,
                    "provider": "telegram",
                    "direction": "credit",
                    "config_id": 77,
                    "server_id": 8,
                    "payload": "must-never-leak",
                },
                occurred_at=NOW + timedelta(minutes=7),
            )
        )
        session.add(
            VPNOperation(
                operation_id="82000000-0000-4000-8000-000000000001",
                owner_id=target.id,
                config_id=None,
                config_name="private-config-name",
                server_id=None,
                kind="create",
                payload={"config": "must-never-leak"},
                status="succeeded",
                attempts=1,
                created_at=NOW + timedelta(minutes=5),
            )
        )
        session.add(
            AdminAuditEvent(
                actor_user_id=admin.id,
                action="balance.adjustment_applied",
                target_type="user",
                target_id=str(target.id),
                request_id="timeline-request",
                correlation_id="timeline-correlation",
                details={
                    "outcome": "completed",
                    "amount": "20.00",
                    "direction": "credit",
                    "target_balance": "25.00",
                    "comment": "private support note",
                    "reason_code": "support_correction",
                    "token": "must-never-leak",
                },
                created_at=NOW + timedelta(minutes=6),
            )
        )
    return target, admin


@pytest.mark.asyncio
async def test_unified_timeline_filters_paginates_and_redacts(sessionmaker):
    target, _ = await _seed_timeline(sessionmaker)
    service = AdminUserTimelineService(uow)
    page = await service.list_timeline(
        target.id,
        include_finance=True,
        include_referral=True,
        include_vpn=True,
        include_admin=True,
        limit=100,
    )
    assert page is not None
    assert {item["source"] for item in page["items"]} == {
        "bot",
        "ledger",
        "payment",
        "referral",
        "vpn",
        "admin",
        "user",
    }
    assert all(
        set(item)
        == {
            "id",
            "source",
            "category",
            "action",
            "result",
            "occurred_at",
            "title",
            "description",
            "metadata",
            "actor",
        }
        for item in page["items"]
    )
    serialized = json.dumps(page, ensure_ascii=False)
    assert "private-payload" not in serialized
    assert "must-never-leak" not in serialized
    assert "private-config-name" not in serialized

    bot = await service.list_timeline(
        target.id,
        category="bot",
        action="finance.payment_amount_select",
        result="handled",
        occurred_from=NOW + timedelta(minutes=6),
        occurred_to=NOW + timedelta(minutes=8),
        include_finance=True,
        include_vpn=True,
    )
    assert bot is not None and bot["total"] == 1
    assert bot["items"][0]["id"].startswith("bot:")
    assert bot["items"][0]["metadata"] == {
        "direction": "credit",
        "provider": "telegram",
        "amount_rub": 300,
        "config_id": 77,
        "server_id": 8,
    }

    first = await service.list_timeline(
        target.id,
        include_finance=True,
        include_referral=True,
        include_vpn=True,
        include_admin=True,
        limit=2,
        offset=0,
    )
    second = await service.list_timeline(
        target.id,
        include_finance=True,
        include_referral=True,
        include_vpn=True,
        include_admin=True,
        limit=2,
        offset=2,
    )
    assert first is not None and second is not None
    assert first["total"] == second["total"] == page["total"]
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )
    assert await service.list_timeline(999_999) is None
    with pytest.raises(ValueError, match="earlier"):
        await service.list_timeline(
            target.id,
            occurred_from=NOW,
            occurred_to=NOW,
        )


@pytest.mark.asyncio
async def test_timeline_permissions_shape_bot_and_admin_metadata(sessionmaker):
    target, _ = await _seed_timeline(sessionmaker)
    page = await AdminUserTimelineService(uow).list_timeline(
        target.id,
        include_finance=False,
        include_referral=False,
        include_vpn=False,
        include_admin=True,
    )
    assert page is not None
    assert {item["source"] for item in page["items"]} == {"bot", "admin", "user"}
    bot = next(item for item in page["items"] if item["source"] == "bot")
    assert bot["metadata"] == {}
    admin = next(item for item in page["items"] if item["source"] == "admin")
    assert admin["metadata"] == {
        "outcome": "completed",
        "reason_code": "support_correction",
    }


def _principal(role: AdminRole) -> AdminPrincipal:
    return AdminPrincipal(
        user_id=999,
        username=role.value,
        role=role,
        session_id=1,
        csrf_token_hash=hashlib.sha256(b"csrf").hexdigest(),
        expires_at=NOW + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_timeline_endpoint_requires_audit_and_includes_selected_to_date(
    sessionmaker,
):
    target, _ = await _seed_timeline(sessionmaker)

    support_app = FastAPI()
    support_app.include_router(users_router)
    support_app.dependency_overrides[get_admin_principal] = lambda: _principal(
        AdminRole.SUPPORT
    )
    async with AsyncClient(
        transport=ASGITransport(app=support_app), base_url="https://admin.test"
    ) as client:
        denied = await client.get(f"/api/admin/v1/users/{target.id}/timeline")
    assert denied.status_code == 403

    owner_app = FastAPI()
    owner_app.include_router(users_router)
    owner_app.dependency_overrides[get_admin_principal] = lambda: _principal(
        AdminRole.OWNER
    )
    async with AsyncClient(
        transport=ASGITransport(app=owner_app), base_url="https://admin.test"
    ) as client:
        response = await client.get(
            f"/api/admin/v1/users/{target.id}/timeline",
            params={"category": "bot", "to": "2026-07-14"},
        )
        bounded = await client.get(
            f"/api/admin/v1/users/{target.id}/timeline",
            params={"offset": 10_001},
        )
        overflowing = await client.get(
            f"/api/admin/v1/users/{target.id}/timeline",
            params={"to": "9999-12-31"},
        )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert bounded.status_code == 422
    assert overflowing.status_code == 400


@pytest.mark.asyncio
async def test_bot_audit_history_prevents_destructive_user_delete(sessionmaker):
    user = await UserService(uow).register(830_001, username="retained")
    async with sessionmaker() as session, session.begin():
        session.add(
            TelegramUserActionEvent(
                user_id=user.id,
                source_update_id=830,
                category="bot",
                action="navigation.start",
                result="handled",
                metadata_json={},
                occurred_at=NOW,
            )
        )
    assert await UserService(uow).delete(user.id) is False
    async with sessionmaker() as session:
        assert await session.get(User, user.id) is not None


@pytest.mark.asyncio
async def test_bot_action_repo_exposes_no_mutation_or_uncontrolled_add(sessionmaker):
    user = await UserService(uow).register(830_002, username="append-only")
    async with uow() as repos:
        event, created = await repos.telegram_user_actions.append_once(
            user_id=user.id,
            source_update_id=831,
            action="navigation.start",
            result="handled",
            metadata={},
            occurred_at=NOW,
        )
        assert created
        with pytest.raises(InvalidOperationError, match="only be appended"):
            await repos.telegram_user_actions.add(event)
        with pytest.raises(InvalidOperationError, match="immutable"):
            await repos.telegram_user_actions.delete(id=event.id)

    async with sessionmaker() as session:
        assert await session.scalar(
            select(TelegramUserActionEvent.id).where(
                TelegramUserActionEvent.source_update_id == 831
            )
        )
