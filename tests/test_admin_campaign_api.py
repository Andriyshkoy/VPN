"""HTTP campaign tests use local snapshots/outbox records and never send messages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from admin.request_context import RequestContextMiddleware
from admin.routers.admin_v1_campaigns import router
from admin.security import CSRF_COOKIE_NAME, AdminPrincipal, get_admin_principal
from core.db.models import (
    AdminAuditEvent,
    AdminRole,
    AdminUser,
    NotificationOutbox,
    User,
)
from core.db.models.campaign import AdminCampaign, CampaignRecipient
from core.db.repo.billing import BillingRepo
from core.services.campaigns import CampaignDeliveryService, CampaignService

TEXT = "Private campaign message 7e2b81: service update."
ROOT = "/api/admin/v1/campaigns"


async def _principal(sessionmaker, role=AdminRole.OWNER):
    async with sessionmaker() as session, session.begin():
        actor = AdminUser(
            username=f"campaign-{role.value}",
            password_hash="$2b$12$unused-but-never-authenticated",
            role=role.value,
        )
        session.add(actor)
        await session.flush()
    csrf = "campaign-test-csrf-token"
    return (
        AdminPrincipal(
            user_id=actor.id,
            username=actor.username,
            role=role,
            session_id=1,
            csrf_token_hash=hashlib.sha256(csrf.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
        csrf,
    )


def _app(principal):
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


def _headers(csrf, key="campaign-launch-key-01"):
    return {"X-CSRF-Token": csrf, "Idempotency-Key": key}


def _draft(audience=None, *, text=TEXT):
    return {
        "name": "Service update",
        "text": text,
        "audience": audience or {"kind": "all", "user_ids": []},
    }


def _launch(preview, **overrides):
    return {
        "revision": preview["revision"],
        "preview_token": preview["preview_token"],
        "expected_recipient_count": preview["eligible_count"],
        "confirmation": "SEND",
        "reason": "Reviewed message and recipient snapshot",
        **overrides,
    }


async def _users(sessionmaker):
    async with sessionmaker() as session, session.begin():
        active = User(tg_id=910_001, username="sensitive-active-handle")
        blocked = User(
            tg_id=910_002,
            username="sensitive-blocked-handle",
            telegram_delivery_status="blocked",
        )
        deactivated = User(tg_id=910_003, telegram_delivery_status="deactivated")
        second = User(tg_id=910_004, username="sensitive-second-handle")
        session.add_all([active, blocked, deactivated, second])
        await session.flush()
    return active, blocked, deactivated, second


async def _new_campaign(client, csrf, audience=None):
    response = await client.post(ROOT, headers=_headers(csrf), json=_draft(audience))
    assert response.status_code == 201, response.text
    return response.json()


async def _preview(client, csrf, campaign):
    response = await client.post(
        f"{ROOT}/{campaign['id']}/preview", headers=_headers(csrf), json={}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _counts(sessionmaker):
    async with sessionmaker() as session:
        return (
            await session.scalar(select(func.count()).select_from(CampaignRecipient)),
            await session.scalar(select(func.count()).select_from(NotificationOutbox)),
        )


@pytest.mark.asyncio
async def test_campaign_review_redacts_recipients_and_launch_freezes_exact_snapshot(
    sessionmaker,
):
    active, blocked, deactivated, second = await _users(sessionmaker)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(client, csrf)
        preview = await _preview(client, csrf, campaign)
        assert preview["campaign_id"] == campaign["id"]
        assert preview["eligible_count"] == 2
        assert preview["skipped_count"] == 2
        assert preview["missing_count"] == 0
        assert preview["sample"] == [
            {"user_id": active.id, "display": f"user #{active.id}"},
            {"user_id": second.id, "display": f"user #{second.id}"},
        ]
        serialized = json.dumps(preview)
        assert "tg_id" not in serialized
        assert "username" not in serialized
        assert "sensitive-" not in serialized
        assert str(active.tg_id) not in serialized
        assert await _counts(sessionmaker) == (0, 0)

        launch_path = f"{ROOT}/{campaign['id']}/launch"
        launched = await client.post(
            launch_path, headers=_headers(csrf), json=_launch(preview)
        )
        assert launched.status_code == 200, launched.text
        assert launched.json()["status"] == "queued"
        assert launched.json()["counts"]["total"] == 2
        replay = await client.post(
            launch_path, headers=_headers(csrf), json=_launch(preview)
        )
        assert replay.status_code == 200, replay.text
        assert replay.json() == launched.json()
        assert await _counts(sessionmaker) == (2, 2)

        async with sessionmaker() as session, session.begin():
            session.add(User(tg_id=910_005, username="joined-after-launch"))
            (await session.get(User, blocked.id)).telegram_delivery_status = "active"
        detail = await client.get(f"{ROOT}/{campaign['id']}")
        assert detail.status_code == 200
        assert detail.json()["counts"]["total"] == 2
        assert "preview_token" not in detail.text
        assert "tg_id" not in detail.text

    async with sessionmaker() as session:
        recipients = list((await session.scalars(select(CampaignRecipient))).all())
        assert {r.user_id for r in recipients} == {active.id, second.id}
        assert {r.tg_id_snapshot for r in recipients} == {active.tg_id, second.tg_id}
        assert deactivated.id not in {r.user_id for r in recipients}
        outbox = list((await session.scalars(select(NotificationOutbox))).all())
        assert len({item.dedupe_key for item in outbox}) == 2
        assert all(item.text == TEXT and item.status == "pending" for item in outbox)
        events = list((await session.scalars(select(AdminAuditEvent))).all())
        assert {event.action for event in events} >= {
            "campaign.draft_created",
            "campaign.launched",
        }
        assert all(event.actor_user_id == principal.user_id for event in events)
        for event in events:
            assert (
                event.details["text_sha256"]
                == hashlib.sha256(TEXT.encode()).hexdigest()
            )
            assert event.details["text_length"] == len(TEXT)
            assert TEXT not in json.dumps(event.details)
            assert "sensitive-" not in json.dumps(event.details)


@pytest.mark.asyncio
async def test_selected_campaign_preview_reports_missing_and_skipped_users(
    sessionmaker,
):
    active, blocked, _, second = await _users(sessionmaker)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(
            client,
            csrf,
            {"kind": "selected", "user_ids": [active.id, blocked.id, 999999]},
        )
        preview = await _preview(client, csrf, campaign)
        assert preview["eligible_count"] == 1
        assert preview["skipped_count"] == 1
        assert preview["missing_count"] == 1
        launched = await client.post(
            f"{ROOT}/{campaign['id']}/launch",
            headers=_headers(csrf),
            json=_launch(preview),
        )
        assert launched.status_code == 200, launched.text
    async with sessionmaker() as session:
        recipients = list((await session.scalars(select(CampaignRecipient))).all())
        assert [recipient.user_id for recipient in recipients] == [active.id]
        assert second.id not in [recipient.user_id for recipient in recipients]


@pytest.mark.parametrize("confirmation", [None, "send", "CONFIRM", True])
@pytest.mark.asyncio
async def test_campaign_launch_requires_explicit_typed_review(
    sessionmaker, confirmation
):
    await _users(sessionmaker)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(client, csrf)
        preview = await _preview(client, csrf, campaign)
        body = _launch(preview, confirmation=confirmation)
        if confirmation is None:
            body.pop("confirmation")
        response = await client.post(
            f"{ROOT}/{campaign['id']}/launch",
            headers=_headers(csrf),
            json=body,
        )
    assert response.status_code == 422, response.text
    assert await _counts(sessionmaker) == (0, 0)


@pytest.mark.parametrize(
    "change", ["count", "token", "new_user", "blocked_user", "edit", "expired"]
)
@pytest.mark.asyncio
async def test_campaign_launch_rejects_stale_or_changed_review(sessionmaker, change):
    active, _, _, _ = await _users(sessionmaker)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(client, csrf)
        preview = await _preview(client, csrf, campaign)
        body = _launch(preview)
        if change == "count":
            body["expected_recipient_count"] += 1
        elif change == "token":
            body["preview_token"] = "never-reviewed-token-abcdef"
        elif change in {"new_user", "blocked_user"}:
            async with sessionmaker() as session, session.begin():
                if change == "new_user":
                    session.add(User(tg_id=910_099))
                else:
                    (await session.get(User, active.id)).telegram_delivery_status = (
                        "blocked"
                    )
        elif change == "expired":
            async with sessionmaker() as session, session.begin():
                stored = await session.scalar(
                    select(AdminCampaign).where(
                        AdminCampaign.public_id == campaign["id"]
                    )
                )
                stored.previewed_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        else:
            updated = await client.patch(
                f"{ROOT}/{campaign['id']}",
                headers=_headers(csrf),
                json={**_draft(text="Changed after preview"), "revision": 1},
            )
            assert updated.status_code == 200, updated.text
        response = await client.post(
            f"{ROOT}/{campaign['id']}/launch",
            headers=_headers(csrf),
            json=body,
        )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "campaign_preview_stale"
    assert await _counts(sessionmaker) == (0, 0)


@pytest.mark.asyncio
async def test_campaign_cancel_blocks_outbox_dispatch_and_delivery_claim(sessionmaker):
    await _users(sessionmaker)
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(client, csrf)
        preview = await _preview(client, csrf, campaign)
        response = await client.post(
            f"{ROOT}/{campaign['id']}/launch",
            headers=_headers(csrf),
            json=_launch(preview),
        )
        assert response.status_code == 200, response.text
        async with sessionmaker() as session, session.begin():
            outbox = list((await session.scalars(select(NotificationOutbox))).all())
            for item in outbox:
                item.status = "queued"
            already_queued = [
                (item.dedupe_key, item.chat_id, item.text) for item in outbox
            ]
        cancellation = await client.post(
            f"{ROOT}/{campaign['id']}/cancel",
            headers=_headers(csrf, "campaign-cancel-key-01"),
            json={"reason": "Reviewed cancellation before dispatch"},
        )
        assert cancellation.status_code == 200, cancellation.text
        assert cancellation.json()["status"] == "cancelled"
        assert cancellation.json()["counts"]["cancelled"] == 2
        retry = await client.post(
            f"{ROOT}/{campaign['id']}/retry",
            headers=_headers(csrf, "campaign-retry-key-01"),
            json={"reason": "Retry cancellation must be rejected"},
        )
        assert retry.status_code == 409

    async with sessionmaker() as session, session.begin():
        assert await BillingRepo(session).claim_notification_outbox() == []
    guard = CampaignDeliveryService(sessionmaker)
    for notification_id, chat_id, text in already_queued:
        claim = await guard.claim(notification_id, chat_id=chat_id, text=text)
        assert claim.allowed is False
        assert claim.status == "cancelled"
    assert await _counts(sessionmaker) == (2, 2)


@pytest.mark.asyncio
async def test_campaign_retry_only_requeues_definitive_failed_eligible_recipients(
    sessionmaker,
):
    await _users(sessionmaker)
    async with sessionmaker() as session, session.begin():
        session.add_all([User(tg_id=910_100), User(tg_id=910_101)])
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        campaign = await _new_campaign(client, csrf)
        preview = await _preview(client, csrf, campaign)
        response = await client.post(
            f"{ROOT}/{campaign['id']}/launch",
            headers=_headers(csrf),
            json=_launch(preview),
        )
        assert response.status_code == 200, response.text
        async with sessionmaker() as session, session.begin():
            recipients = list(
                (
                    await session.scalars(
                        select(CampaignRecipient).order_by(CampaignRecipient.id)
                    )
                ).all()
            )
            assert len(recipients) == 4
            for recipient, status in zip(
                recipients, ["delivered", "unknown", "failed", "failed"], strict=True
            ):
                recipient.status = status
                (await session.get(NotificationOutbox, recipient.outbox_id)).status = (
                    status
                )
            (
                await session.get(User, recipients[-1].user_id)
            ).telegram_delivery_status = "blocked"
        retry_path = f"{ROOT}/{campaign['id']}/retry"
        retry_body = {"reason": "Reviewed definitive failures before retry"}
        retry_headers = _headers(csrf, "campaign-failed-only-retry-01")
        retried = await client.post(retry_path, headers=retry_headers, json=retry_body)
        assert retried.status_code == 200, retried.text
        counts = retried.json()["counts"]
        assert counts["total"] == 4
        assert counts["pending"] == 1
        assert counts["delivered"] == 1
        assert counts["unknown"] == 1
        assert counts["failed"] == 1
        replay = await client.post(retry_path, headers=retry_headers, json=retry_body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == retried.json()
    assert await _counts(sessionmaker) == (4, 5)


@pytest.mark.parametrize(
    "role", [AdminRole.SUPPORT, AdminRole.FINANCE, AdminRole.OPS, AdminRole.VIEWER]
)
@pytest.mark.asyncio
async def test_campaign_operations_require_owner_permissions(sessionmaker, role):
    principal, csrf = await _principal(sessionmaker, role)
    async with sessionmaker() as session, session.begin():
        campaign = await CampaignService(session).create(**_draft())
    path = f"{ROOT}/{campaign['id']}"
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        requests = [
            ("GET", ROOT, None),
            ("GET", path, None),
            ("POST", ROOT, _draft()),
            ("PATCH", path, {**_draft(), "revision": 1}),
            ("POST", f"{path}/preview", {}),
            (
                "POST",
                f"{path}/launch",
                _launch(
                    {"revision": 1, "preview_token": "x" * 32, "eligible_count": 1}
                ),
            ),
            ("POST", f"{path}/cancel", {"reason": "Attempt owner operation"}),
            ("POST", f"{path}/retry", {"reason": "Attempt owner operation"}),
        ]
        for method, url, body in requests:
            response = await client.request(
                method, url, headers=_headers(csrf), json=body
            )
            assert response.status_code == 403, (method, url, response.text)
    assert await _counts(sessionmaker) == (0, 0)


@pytest.mark.parametrize(
    "csrf_mode", ["absent", "header_only", "mismatch", "cross_origin"]
)
@pytest.mark.asyncio
async def test_campaign_mutations_require_csrf_and_same_origin(sessionmaker, csrf_mode):
    principal, csrf = await _principal(sessionmaker)
    headers = {"Idempotency-Key": "campaign-csrf-key-01"}
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        if csrf_mode != "absent":
            headers["X-CSRF-Token"] = csrf
        if csrf_mode in {"mismatch", "cross_origin"}:
            client.cookies.set(
                CSRF_COOKIE_NAME, "wrong-token" if csrf_mode == "mismatch" else csrf
            )
        if csrf_mode == "cross_origin":
            headers["Origin"] = "https://other.test"
        response = await client.post(ROOT, headers=headers, json=_draft())
    assert response.status_code == 403
    async with sessionmaker() as session:
        assert (
            await session.scalar(select(func.count()).select_from(AdminCampaign)) == 0
        )


@pytest.mark.parametrize(
    "audience",
    [
        {"kind": "selected", "user_ids": []},
        {"kind": "all", "user_ids": [1]},
        {"kind": "selected", "user_ids": [True]},
        {"kind": "selected", "user_ids": ["1"]},
        {"kind": "selected", "user_ids": [1, 1]},
    ],
)
@pytest.mark.asyncio
async def test_campaign_audience_rejects_ambiguous_selection(sessionmaker, audience):
    principal, csrf = await _principal(sessionmaker)
    async with AsyncClient(
        transport=ASGITransport(app=_app(principal)), base_url="https://admin.test"
    ) as client:
        client.cookies.set(CSRF_COOKIE_NAME, csrf)
        response = await client.post(
            ROOT, headers=_headers(csrf), json=_draft(audience)
        )
    assert response.status_code == 422, response.text
