from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import ClassVar

import pytest
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
)
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select

from bot.middlewares import InviteOnlyAccessMiddleware
from core.db.models.user import User as UserModel
from core.db.unit_of_work import uow
from core.exceptions import InvalidOperationError
from core.services import UserService


async def _orm_user(sessionmaker, tg_id: int) -> UserModel | None:
    async with sessionmaker() as session:
        return await session.scalar(select(UserModel).where(UserModel.tg_id == tg_id))


@pytest.mark.asyncio
async def test_unknown_user_is_created_only_by_valid_opaque_invite(sessionmaker):
    service = UserService(uow)
    inviter = await service.register(1001, username="inviter")
    inviter_orm = await _orm_user(sessionmaker, inviter.tg_id)
    assert inviter_orm is not None

    assert (
        await service.register_invited(
            2001,
            username="legacy",
            referral_code=str(inviter.tg_id),
        )
        is None
    )
    assert (
        await service.register_invited(
            2002,
            username="unknown",
            referral_code=f"ref_{'x' * 32}",
        )
        is None
    )
    assert (
        await service.register_invited(
            2003,
            username="missing",
            referral_code=None,
        )
        is None
    )

    invited = await service.register_invited(
        2004,
        username="invited",
        referral_code=f"ref_{inviter_orm.referral_code}",
    )
    assert invited is not None
    invited_orm = await _orm_user(sessionmaker, invited.tg_id)
    assert invited_orm is not None
    assert invited_orm.referred_by_id == inviter.id

    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(UserModel)) == 2


@pytest.mark.asyncio
async def test_existing_user_is_grandfathered_and_attribution_is_not_rewritten(
    sessionmaker,
):
    service = UserService(uow)
    first_inviter = await service.register(3001)
    second_inviter = await service.register(3002)
    first_orm = await _orm_user(sessionmaker, first_inviter.tg_id)
    second_orm = await _orm_user(sessionmaker, second_inviter.tg_id)
    assert first_orm is not None and second_orm is not None

    invited = await service.register_invited(
        3003,
        username="before",
        referral_code=f"ref_{first_orm.referral_code}",
    )
    assert invited is not None

    # Existing accounts need no invitation and a later link cannot change the
    # referrer chosen by the original INSERT.
    existing = await service.register_invited(
        3003,
        username="after",
        referral_code=f"ref_{second_orm.referral_code}",
    )
    assert existing is not None
    existing_without_payload = await service.register_invited(
        3003,
        username="after",
        referral_code=None,
    )
    assert existing_without_payload is not None

    stored = await _orm_user(sessionmaker, 3003)
    assert stored is not None
    assert stored.username == "after"
    assert stored.referred_by_id == first_inviter.id

    with pytest.raises(InvalidOperationError, match="immutable"):
        await service.update(stored.id, referred_by_id=second_inviter.id)
    with pytest.raises(InvalidOperationError, match="immutable"):
        await service.update(stored.id, referral_code="x" * 32)


class _AccessService:
    def __init__(self, existing_ids: set[int] | None = None):
        self.existing_ids = existing_ids or set()

    async def find_by_tg_id(self, tg_id: int, **kwargs):
        if tg_id in self.existing_ids:
            return SimpleNamespace(id=tg_id, tg_id=tg_id)
        return None


class _Callback(CallbackQuery):
    acknowledgements: ClassVar[list[tuple[tuple, dict]]] = []

    async def answer(self, *args, **kwargs):
        self.acknowledgements.append((args, kwargs))


def _message(
    text: str | None,
    *,
    successful_payment=None,
    user_id: int = 20,
) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=10, type="private"),
        from_user=TelegramUser(
            id=user_id,
            is_bot=False,
            first_name="Unknown",
            username="unknown",
        ),
        text=text,
        successful_payment=successful_payment,
    )


@pytest.mark.asyncio
async def test_only_private_message_reactivates_blocked_delivery_status(sessionmaker):
    service = UserService(uow)
    user = await service.register(3_100)
    async with uow() as repos:
        await repos["users"].set_telegram_delivery_status(
            user.tg_id,
            delivery_status="blocked",
            error="bot was blocked",
        )

    # A direct existing-account lookup must not infer message reachability.
    existing = await service.register_invited(
        user.tg_id,
        username="still-blocked",
        referral_code=None,
    )
    assert existing is not None
    stored = await _orm_user(sessionmaker, user.tg_id)
    assert stored is not None
    assert stored.telegram_delivery_status == "blocked"

    middleware = InviteOnlyAccessMiddleware(service)

    async def existing_handler(event, data):
        return await service.register_invited(
            event.from_user.id,
            username=event.from_user.username,
            referral_code=None,
        )

    # A callback can be delivered from an old inline keyboard while fresh
    # private messages are still blocked.
    callback = CallbackQuery(
        id="existing-callback",
        from_user=TelegramUser(
            id=user.tg_id,
            is_bot=False,
            first_name="Existing",
            username="existing",
        ),
        chat_instance="chat-instance",
        data="balance_history:0",
        message=_message("old keyboard", user_id=user.tg_id),
    )
    assert await middleware(existing_handler, callback, {}) is not None
    stored = await _orm_user(sessionmaker, user.tg_id)
    assert stored is not None
    assert stored.telegram_delivery_status == "blocked"

    # Pre-checkout is a payment protocol event, not proof that sendMessage is
    # available for this user.
    pre_checkout = PreCheckoutQuery(
        id="existing-checkout",
        from_user=TelegramUser(
            id=user.tg_id,
            is_bot=False,
            first_name="Existing",
            username="existing",
        ),
        currency="RUB",
        total_amount=10_000,
        invoice_payload="topup:intent-id",
    )
    assert await middleware(existing_handler, pre_checkout, {}) is not None
    stored = await _orm_user(sessionmaker, user.tg_id)
    assert stored is not None
    assert stored.telegram_delivery_status == "blocked"

    # A private inbound message is the only update type that establishes that
    # the user can currently talk to the bot.
    assert (
        await middleware(
            existing_handler,
            _message("/menu", user_id=user.tg_id),
            {},
        )
        is not None
    )
    stored = await _orm_user(sessionmaker, user.tg_id)
    assert stored is not None
    assert stored.telegram_delivery_status == "active"
    assert stored.telegram_blocked_at is None
    assert stored.telegram_last_delivery_error is None


@pytest.mark.asyncio
async def test_access_middleware_grandfathers_existing_user():
    middleware = InviteOnlyAccessMiddleware(_AccessService({20}))
    calls = []
    data = {}

    async def handler(event, handler_data):
        calls.append(event)
        return "handled"

    result = await middleware(handler, _message("/menu"), data)

    assert result == "handled"
    assert len(calls) == 1
    assert data["current_user"].tg_id == 20


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/start 123456",
        f"/start {'x' * 32}",
        f"/start ref_{'x' * 31}",
        "/menu",
        "hello",
    ],
)
async def test_access_middleware_silently_drops_unknown_messages(text):
    middleware = InviteOnlyAccessMiddleware(_AccessService())
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    assert await middleware(handler, _message(text), {}) is None
    assert called is False


@pytest.mark.asyncio
async def test_access_middleware_allows_only_canonical_private_invited_start():
    middleware = InviteOnlyAccessMiddleware(_AccessService())
    called = False

    async def handler(event, data):
        nonlocal called
        called = True
        return "start"

    result = await middleware(
        handler,
        _message(f"/start ref_{'A_b-' * 8}"),
        {},
    )

    assert result == "start"
    assert called is True


@pytest.mark.asyncio
async def test_access_middleware_blank_acks_unknown_callback():
    middleware = InviteOnlyAccessMiddleware(_AccessService())
    _Callback.acknowledgements = []
    callback = _Callback(
        id="callback-id",
        from_user=TelegramUser(id=20, is_bot=False, first_name="Unknown"),
        chat_instance="chat-instance",
        data="cfg:1",
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    assert await middleware(handler, callback, {}) is None
    assert called is False
    assert callback.acknowledgements == [((), {})]


@pytest.mark.asyncio
async def test_access_middleware_rejects_unknown_pre_checkout():
    middleware = InviteOnlyAccessMiddleware(_AccessService())
    answers = []

    class Bot:
        async def answer_pre_checkout_query(self, query_id, **kwargs):
            answers.append((query_id, kwargs))

    query = PreCheckoutQuery(
        id="checkout-id",
        from_user=TelegramUser(id=20, is_bot=False, first_name="Unknown"),
        currency="RUB",
        total_amount=10_000,
        invoice_payload="topup:intent-id",
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True

    assert await middleware(handler, query, {"bot": Bot()}) is None
    assert called is False
    assert answers == [
        (
            "checkout-id",
            {
                "ok": False,
                "error_message": ("Доступ к боту возможен только по приглашению."),
            },
        )
    ]


@pytest.mark.asyncio
async def test_access_middleware_fails_pre_checkout_closed_on_lookup_error():
    class BrokenAccessService:
        async def find_by_tg_id(self, tg_id: int, **kwargs):
            raise ConnectionError("database unavailable")

    middleware = InviteOnlyAccessMiddleware(BrokenAccessService())
    answers = []

    class Bot:
        async def answer_pre_checkout_query(self, query_id, **kwargs):
            answers.append((query_id, kwargs))

    query = PreCheckoutQuery(
        id="checkout-error",
        from_user=TelegramUser(id=20, is_bot=False, first_name="Unknown"),
        currency="RUB",
        total_amount=10_000,
        invoice_payload="topup:intent-id",
    )

    assert await middleware(lambda *_: None, query, {"bot": Bot()}) is None
    assert answers[0][1]["ok"] is False


@pytest.mark.asyncio
async def test_access_middleware_keeps_regular_update_retryable_on_lookup_error():
    class BrokenAccessService:
        async def find_by_tg_id(self, tg_id: int, **kwargs):
            raise ConnectionError("database unavailable")

    middleware = InviteOnlyAccessMiddleware(BrokenAccessService())

    with pytest.raises(ConnectionError, match="database unavailable"):
        await middleware(lambda *_: None, _message("/menu"), {})


@pytest.mark.asyncio
async def test_access_middleware_does_not_swallow_captured_unknown_payment():
    middleware = InviteOnlyAccessMiddleware(_AccessService())
    payment = SuccessfulPayment(
        currency="RUB",
        total_amount=10_000,
        invoice_payload="topup:intent-id",
        telegram_payment_charge_id="telegram-charge",
        provider_payment_charge_id="provider-charge",
    )
    called = False

    async def handler(event, data):
        nonlocal called
        called = True
        return "payment"

    result = await middleware(
        handler,
        _message(None, successful_payment=payment),
        {},
    )

    assert result == "payment"
    assert called is True
