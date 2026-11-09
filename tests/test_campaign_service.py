from datetime import timedelta

import pytest
from sqlalchemy import func, select

from core.config import settings
from core.db.models import (
    AdminCampaign,
    CampaignOperation,
    CampaignRecipient,
    NotificationOutbox,
    User,
)
from core.services.campaigns import (
    PREVIEW_TTL,
    CampaignConflict,
    CampaignDeliveryService,
    CampaignService,
    CampaignValidationError,
    now_utc,
)


async def add_users(maker):
    async with maker() as session, session.begin():
        users = [
            User(tg_id=101, username="private-name"),
            User(tg_id=102),
            User(tg_id=103, telegram_delivery_status="blocked"),
            User(tg_id=-104),
        ]
        session.add_all(users)
        await session.flush()
        return [u.id for u in users]


async def prepare(maker, *, audience=None, text="Plain <text> & test"):
    async with maker() as session, session.begin():
        service = CampaignService(session)
        campaign = await service.create(
            name="Service restored",
            text=text,
            audience=audience or {"kind": "all", "user_ids": []},
        )
        preview = await service.preview(campaign["id"])
        return campaign, preview


async def launch(maker, campaign, preview, *, key="launch-test-key"):
    async with maker() as session, session.begin():
        return await CampaignService(session).launch(
            campaign["id"],
            revision=preview["revision"],
            preview_token=preview["preview_token"],
            expected_recipient_count=preview["eligible_count"],
            idempotency_key=key,
        )


async def recipient_rows(maker):
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(CampaignRecipient).order_by(CampaignRecipient.id)
                )
            ).all()
        )


async def test_preview_redacts_and_freezes_exact_recipients(sessionmaker):
    ids = await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    assert preview["eligible_count"] == 2
    assert preview["skipped_count"] == 2
    assert preview["sample"] == [
        {"user_id": i, "display": f"user #{i}"} for i in ids[:2]
    ]
    assert "private-name" not in str(preview)
    first = await launch(sessionmaker, campaign, preview)
    second = await launch(sessionmaker, campaign, preview)
    assert first["counts"] == second["counts"]
    assert first["counts"]["total"] == 2
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 2
        assert await session.scalar(select(func.count(CampaignOperation.id))) == 1
        outbox = await session.scalar(
            select(NotificationOutbox).order_by(NotificationOutbox.id)
        )
        assert outbox.text == campaign["text"]


async def test_delivery_pause_is_observed_and_launch_only_queues(
    sessionmaker, monkeypatch
):
    await add_users(sessionmaker)
    monkeypatch.setattr(settings, "notifications_enabled", False)
    campaign, preview = await prepare(sessionmaker)
    assert campaign["delivery_paused"] is True
    launched = await launch(sessionmaker, campaign, preview)
    assert launched["delivery_paused"] is True
    assert launched["status"] == "queued"
    assert launched["counts"]["total"] == launched["counts"]["pending"] == 2
    async with sessionmaker() as session:
        assert set(await session.scalars(select(NotificationOutbox.status))) == {
            "pending"
        }

    # The response reflects the current process setting rather than the flag
    # at draft creation/launch, without changing the stored queue state.
    monkeypatch.setattr(settings, "notifications_enabled", True)
    async with sessionmaker() as session:
        service = CampaignService(session)
        current = await service.get(campaign["id"])
        page = await service.list()
        assert current["delivery_paused"] is False
        assert page["items"][0]["delivery_paused"] is False
        assert current["status"] == "queued"
        assert current["counts"]["delivered"] == 0


async def test_selected_audience_skips_blocked_missing_and_deduplicates(sessionmaker):
    ids = await add_users(sessionmaker)
    _, preview = await prepare(
        sessionmaker,
        audience={"kind": "selected", "user_ids": [ids[0], ids[0], ids[2], 9999]},
    )
    assert (
        preview["eligible_count"],
        preview["skipped_count"],
        preview["missing_count"],
    ) == (1, 1, 1)


async def test_launch_rejects_changed_eligibility_and_requires_new_review(sessionmaker):
    ids = await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    async with sessionmaker() as session, session.begin():
        (await session.get(User, ids[1])).telegram_delivery_status = "blocked"
    with pytest.raises(CampaignConflict, match="stale"):
        await launch(sessionmaker, campaign, preview)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 0


async def test_launch_rejects_new_eligible_users_since_preview(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    async with sessionmaker() as session, session.begin():
        session.add(User(tg_id=99999))
    with pytest.raises(CampaignConflict, match="stale"):
        await launch(sessionmaker, campaign, preview)


async def test_expired_preview_and_wrong_count_cannot_launch(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    async with sessionmaker() as session, session.begin():
        item = await session.scalar(select(AdminCampaign))
        item.previewed_at = now_utc() - PREVIEW_TTL - timedelta(seconds=1)
    with pytest.raises(CampaignConflict, match="stale"):
        await launch(sessionmaker, campaign, preview)
    async with sessionmaker() as session, session.begin():
        preview = await CampaignService(session).preview(campaign["id"])
    preview["eligible_count"] += 1
    with pytest.raises(CampaignConflict, match="stale"):
        await launch(sessionmaker, campaign, preview)


async def test_launched_content_is_immutable_and_reused_launch_key_conflicts(
    sessionmaker,
):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    await launch(sessionmaker, campaign, preview)
    async with sessionmaker() as session, session.begin():
        with pytest.raises(CampaignConflict):
            await CampaignService(session).update(
                campaign["id"],
                revision=1,
                name="other",
                text="changed",
                audience=campaign["audience"],
            )
    changed = {**preview, "eligible_count": 555}
    with pytest.raises(CampaignConflict, match="different request"):
        await launch(sessionmaker, campaign, changed)
    with pytest.raises(CampaignConflict, match="already"):
        await launch(sessionmaker, campaign, preview, key="different-launch-key")


async def test_edited_draft_invalidates_preview_revision(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    async with sessionmaker() as session, session.begin():
        updated = await CampaignService(session).update(
            campaign["id"],
            revision=1,
            name=campaign["name"],
            text="New content",
            audience=campaign["audience"],
        )
    assert updated["revision"] == 2
    with pytest.raises(CampaignConflict, match="stale"):
        await launch(sessionmaker, campaign, preview)


async def test_cancel_suppresses_already_queued_and_is_idempotent(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    await launch(sessionmaker, campaign, preview)
    rows = await recipient_rows(sessionmaker)
    async with sessionmaker() as session, session.begin():
        outbox = await session.get(NotificationOutbox, rows[0].outbox_id)
        outbox.status = "queued"
    async with sessionmaker() as session, session.begin():
        result = await CampaignService(session).cancel(
            campaign["id"], idempotency_key="cancel-key"
        )
        again = await CampaignService(session).cancel(
            campaign["id"], idempotency_key="cancel-key"
        )
    assert result["counts"]["cancelled"] == 2
    assert result == again
    claim = await CampaignDeliveryService(sessionmaker).claim(
        rows[0].outbox_dedupe_key, chat_id=rows[0].tg_id_snapshot, text=campaign["text"]
    )
    assert not claim.allowed
    async with sessionmaker() as session:
        assert set(
            (await session.scalars(select(NotificationOutbox.status))).all()
        ) == {"cancelled"}


async def test_durable_claim_replay_marks_unknown_without_resend(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    await launch(sessionmaker, campaign, preview)
    row = (await recipient_rows(sessionmaker))[0]
    delivery = CampaignDeliveryService(sessionmaker)
    assert (
        await delivery.claim(
            row.outbox_dedupe_key, chat_id=row.tg_id_snapshot, text=campaign["text"]
        )
    ).allowed
    replay = await delivery.claim(
        row.outbox_dedupe_key, chat_id=row.tg_id_snapshot, text=campaign["text"]
    )
    assert not replay.allowed and replay.status == "unknown"
    # A completed HTTP send whose result was lost is not safe for automatic retry.
    async with sessionmaker() as session, session.begin():
        result = await CampaignService(session).retry_failed(
            campaign["id"], idempotency_key="retry-unknown-key"
        )
    assert result["counts"]["unknown"] == 1
    assert (await recipient_rows(sessionmaker))[0].generation == 0


async def test_retry_only_definitive_failures_remembers_older_keys(sessionmaker):
    await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    await launch(sessionmaker, campaign, preview)
    first, second = await recipient_rows(sessionmaker)
    delivery = CampaignDeliveryService(sessionmaker)
    for row, outcome in [(first, "failed"), (second, "unknown")]:
        await delivery.claim(
            row.outbox_dedupe_key, chat_id=row.tg_id_snapshot, text=campaign["text"]
        )
        await delivery.finish(row.outbox_dedupe_key, status=outcome, error="test_error")
    async with sessionmaker() as session, session.begin():
        await CampaignService(session).retry_failed(
            campaign["id"], idempotency_key="retry-first-key"
        )
    refreshed = (await recipient_rows(sessionmaker))[0]
    assert refreshed.generation == 1
    assert not (
        await delivery.claim(
            first.outbox_dedupe_key, chat_id=first.tg_id_snapshot, text=campaign["text"]
        )
    ).allowed
    await delivery.claim(
        refreshed.outbox_dedupe_key,
        chat_id=refreshed.tg_id_snapshot,
        text=campaign["text"],
    )
    await delivery.finish(
        refreshed.outbox_dedupe_key, status="failed", error="test_error"
    )
    async with sessionmaker() as session, session.begin():
        service = CampaignService(session)
        await service.retry_failed(campaign["id"], idempotency_key="retry-second-key")
        await service.retry_failed(campaign["id"], idempotency_key="retry-first-key")
    rows = await recipient_rows(sessionmaker)
    assert rows[0].generation == 2 and rows[1].generation == 0
    assert rows[1].status == "unknown"
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(NotificationOutbox.id))) == 4


async def test_delivery_rechecks_blocked_recipient_and_exact_payload(sessionmaker):
    ids = await add_users(sessionmaker)
    campaign, preview = await prepare(sessionmaker)
    await launch(sessionmaker, campaign, preview)
    first, second = await recipient_rows(sessionmaker)
    async with sessionmaker() as session, session.begin():
        (await session.get(User, ids[0])).telegram_delivery_status = "blocked"
    delivery = CampaignDeliveryService(sessionmaker)
    assert not (
        await delivery.claim(
            first.outbox_dedupe_key, chat_id=first.tg_id_snapshot, text=campaign["text"]
        )
    ).allowed
    assert not (
        await delivery.claim(
            second.outbox_dedupe_key, chat_id=second.tg_id_snapshot, text="tampered"
        )
    ).allowed
    async with sessionmaker() as session:
        result = await CampaignService(session).get(campaign["id"])
    assert result["status"] == "completed" and result["counts"]["skipped"] == 2


@pytest.mark.parametrize(
    "audience",
    [
        {"kind": "all", "user_ids": [1]},
        {"kind": "selected", "user_ids": []},
        {"kind": "selected", "user_ids": [True]},
        {"kind": "selected", "user_ids": [-1]},
        {"kind": "unexpected"},
    ],
)
async def test_invalid_audience_never_creates_campaign(sessionmaker, audience):
    async with sessionmaker() as session, session.begin():
        with pytest.raises(CampaignValidationError):
            await CampaignService(session).create(
                name="invalid", text="message", audience=audience
            )


async def test_telegram_utf16_limit_and_transaction_rollback(sessionmaker):
    async with sessionmaker() as session:
        with pytest.raises(CampaignValidationError):
            await CampaignService(session).create(
                name="invalid", text="😀" * 2049, audience={"kind": "all"}
            )
        await CampaignService(session).create(
            name="rollback", text="prepared", audience={"kind": "all"}
        )
        await session.rollback()
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count(AdminCampaign.id))) == 0
