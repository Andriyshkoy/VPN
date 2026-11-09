"""Reviewed plain-text broadcasts with frozen audiences and guarded delivery.

Administrative methods participate in the caller's transaction, so RBAC audit
records and mutations can commit together. Delivery methods commit a durable
claim *before* contacting Telegram. Ambiguous delivery is terminal, never an
implicit marketing resend.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import core.db as db
from core.config import settings
from core.db.models.campaign import AdminCampaign, CampaignOperation, CampaignRecipient
from core.db.models.notification_outbox import NotificationOutbox
from core.db.models.user import User
from core.db.repo.campaign import CampaignRepo

PREFIX = "campaign:"
PREVIEW_TTL = timedelta(minutes=15)
TERMINAL = {"delivered", "failed", "unknown", "skipped", "cancelled"}


def now_utc():
    return datetime.now(timezone.utc)


def aware(value):
    return (
        value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


class CampaignError(ValueError):
    code = "campaign_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class CampaignNotFound(CampaignError):
    code = "campaign_not_found"


class CampaignConflict(CampaignError):
    code = "campaign_conflict"


class CampaignValidationError(CampaignError):
    code = "campaign_invalid"


def validate_content(name, text, audience):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
        raise CampaignValidationError("Campaign name must contain 1–160 characters")
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-16-le")) // 2 > 4096
    ):
        raise CampaignValidationError("Message must contain 1–4096 Telegram characters")
    if not isinstance(audience, dict) or audience.get("kind") not in {
        "all",
        "selected",
    }:
        raise CampaignValidationError("Choose all eligible users or selected user IDs")
    ids = audience.get("user_ids", [])
    if (
        not isinstance(ids, list)
        or any(type(i) is not int or i <= 0 for i in ids)
        or len(ids) > 10000
    ):
        raise CampaignValidationError(
            "Selected user IDs must be positive integers (maximum 10000)"
        )
    if audience["kind"] == "selected" and not ids:
        raise CampaignValidationError("Select at least one user")
    if audience["kind"] == "all" and ids:
        raise CampaignValidationError("All-user audience cannot contain selected IDs")
    return name.strip(), text, {"kind": audience["kind"], "user_ids": sorted(set(ids))}


def key_digest(key):
    if not isinstance(key, str) or not 8 <= len(key) <= 200:
        raise CampaignValidationError("Idempotency-Key must contain 8–200 characters")
    return digest(key)


class CampaignService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.repo = CampaignRepo(session)

    async def _get(self, public_id, *, lock=False):
        campaign = await self.repo.get(public_id, lock=lock)
        if campaign is None:
            raise CampaignNotFound("Campaign not found")
        return campaign

    async def _dto(self, campaign):
        counts = await self.repo.counts(campaign.id)
        status = campaign.status
        if status in {"queued", "running"} and not any(
            counts[s] for s in ("pending", "queued", "sending")
        ):
            status = "completed"
        return {
            "id": campaign.public_id,
            "name": campaign.name,
            "text": campaign.text,
            "audience": campaign.audience,
            "status": status,
            # Observed worker switch, not a frozen campaign property. Launch
            # still stages a durable queue while delivery is paused.
            "delivery_paused": not settings.notifications_enabled,
            "revision": campaign.revision,
            "counts": counts,
            "created_at": aware(campaign.created_at).isoformat(),
            "updated_at": aware(campaign.updated_at).isoformat(),
            "launched_at": (
                aware(campaign.launched_at).isoformat()
                if campaign.launched_at
                else None
            ),
            "cancelled_at": (
                aware(campaign.cancelled_at).isoformat()
                if campaign.cancelled_at
                else None
            ),
        }

    async def create(self, *, name, text, audience, actor_user_id=None):
        name, text, audience = validate_content(name, text, audience)
        campaign = AdminCampaign(
            public_id=str(uuid4()),
            name=name,
            text=text,
            audience=audience,
            status="draft",
            revision=1,
            created_by_admin_id=actor_user_id,
            preview_ids=[],
        )
        self.session.add(campaign)
        await self.session.flush()
        return await self._dto(campaign)

    async def update(self, public_id, *, revision, name, text, audience):
        campaign = await self._get(public_id, lock=True)
        if campaign.status != "draft" or campaign.revision != revision:
            raise CampaignConflict("Only the current draft revision can be edited")
        campaign.name, campaign.text, campaign.audience = validate_content(
            name, text, audience
        )
        campaign.revision += 1
        campaign.updated_at = now_utc()
        campaign.preview_token_hash = campaign.preview_digest = (
            campaign.previewed_at
        ) = None
        campaign.preview_ids = []
        await self.session.flush()
        return await self._dto(campaign)

    async def get(self, public_id):
        return await self._dto(await self._get(public_id))

    async def list(self, *, limit=50, offset=0):
        if not 1 <= limit <= 100 or offset < 0:
            raise CampaignValidationError("Invalid pagination")
        total = await self.session.scalar(
            select(func.count()).select_from(AdminCampaign)
        )
        items = (
            await self.session.scalars(
                select(AdminCampaign)
                .order_by(AdminCampaign.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
        return {
            "items": [await self._dto(c) for c in items],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def _snapshot(self, campaign):
        users, skipped, missing = await self.repo.audience(campaign.audience)
        pairs = [[user.id, user.tg_id] for user in users]
        fingerprint = digest(
            {
                "id": campaign.public_id,
                "revision": campaign.revision,
                "text": campaign.text,
                "audience": campaign.audience,
                "recipients": pairs,
                "skipped": skipped,
                "missing": missing,
            }
        )
        return pairs, skipped, missing, fingerprint

    async def preview(self, public_id):
        campaign = await self._get(public_id, lock=True)
        if campaign.status != "draft":
            raise CampaignConflict("Only a draft can be previewed")
        pairs, skipped, missing, fingerprint = await self._snapshot(campaign)
        token = secrets.token_urlsafe(32)
        campaign.preview_token_hash = digest(token)
        campaign.preview_digest = fingerprint
        campaign.preview_ids = pairs
        campaign.previewed_at = now_utc()
        await self.session.flush()
        return {
            "campaign_id": campaign.public_id,
            "revision": campaign.revision,
            "preview_token": token,
            "eligible_count": len(pairs),
            "skipped_count": skipped,
            "missing_count": missing,
            "sample": [
                {"user_id": pair[0], "display": f"user #{pair[0]}"}
                for pair in pairs[:10]
            ],
            "expires_at": (campaign.previewed_at + PREVIEW_TTL).isoformat(),
        }

    async def launch(
        self,
        public_id,
        *,
        revision,
        preview_token,
        expected_recipient_count,
        idempotency_key,
    ):
        campaign = await self._get(public_id, lock=True)
        key_hash = key_digest(idempotency_key)
        request_hash = digest([revision, preview_token, expected_recipient_count])
        old = await self.repo.operation(campaign.id, "launch", key_hash)
        if old:
            if old.request_hash != request_hash:
                raise CampaignConflict(
                    "Idempotency key was already used for a different request"
                )
            return await self._dto(campaign)
        if campaign.status != "draft":
            raise CampaignConflict("Campaign has already been launched or cancelled")
        pairs, _, _, fingerprint = await self._snapshot(campaign)
        if (
            campaign.revision != revision
            or not campaign.preview_token_hash
            or not isinstance(preview_token, str)
            or not hmac.compare_digest(
                campaign.preview_token_hash, digest(preview_token)
            )
            or not campaign.previewed_at
            or now_utc() > aware(campaign.previewed_at) + PREVIEW_TTL
            or campaign.preview_digest != fingerprint
            or campaign.preview_ids != pairs
            or type(expected_recipient_count) is not int
            or len(pairs) != expected_recipient_count
        ):
            raise CampaignConflict(
                "Preview is stale; review the current audience again",
                code="campaign_preview_stale",
            )
        if not pairs:
            raise CampaignValidationError("Campaign has no eligible recipients")
        campaign.status = "queued"
        campaign.launched_at = campaign.updated_at = now_utc()
        recipient_ids = []
        for user_id, tg_id in pairs:
            recipient = CampaignRecipient(
                campaign_id=campaign.id,
                user_id=user_id,
                tg_id_snapshot=tg_id,
                status="pending",
                generation=0,
                attempts=0,
                outbox_dedupe_key=f"{PREFIX}{campaign.public_id}:user:{user_id}:0",
            )
            self.session.add(recipient)
            await self.session.flush()
            await self._outbox(campaign, recipient)
            recipient_ids.append(recipient.id)
        self.session.add(
            CampaignOperation(
                campaign_id=campaign.id,
                kind="launch",
                key_hash=key_hash,
                request_hash=request_hash,
                recipient_ids=recipient_ids,
            )
        )
        await self.session.flush()
        return await self._dto(campaign)

    async def _outbox(self, campaign, recipient):
        outbox = NotificationOutbox(
            dedupe_key=recipient.outbox_dedupe_key,
            chat_id=recipient.tg_id_snapshot,
            text=campaign.text,
            status="pending",
            attempts=0,
        )
        self.session.add(outbox)
        await self.session.flush()
        recipient.outbox_id = outbox.id

    async def cancel(self, public_id, *, idempotency_key=None):
        if idempotency_key is not None:
            key_digest(idempotency_key)
        campaign = await self._get(public_id, lock=True)
        if campaign.status == "cancelled":
            return await self._dto(campaign)
        # In-flight sends cannot be recalled; their eventual outcomes remain visible.
        campaign.status = "cancelled"
        campaign.cancelled_at = campaign.updated_at = now_utc()
        for recipient in await self.repo.recipients(campaign.id, lock=True):
            if recipient.status in {"pending", "queued"}:
                recipient.status = "cancelled"
                recipient.updated_at = now_utc()
                outbox = await self.session.get(NotificationOutbox, recipient.outbox_id)
                if outbox and outbox.status in {"pending", "queued"}:
                    outbox.status = "cancelled"
        await self.session.flush()
        return await self._dto(campaign)

    async def retry_failed(self, public_id, *, idempotency_key):
        campaign = await self._get(public_id, lock=True)
        key_hash = key_digest(idempotency_key)
        if await self.repo.operation(campaign.id, "retry", key_hash):
            return await self._dto(campaign)
        if campaign.status in {"draft", "cancelled"}:
            raise CampaignConflict("This campaign cannot be retried")
        retried = []
        for recipient in await self.repo.recipients(
            campaign.id, status="failed", lock=True
        ):
            user = await self.session.get(User, recipient.user_id)
            if (
                not user
                or user.telegram_delivery_status != "active"
                or user.tg_id != recipient.tg_id_snapshot
            ):
                continue
            recipient.generation += 1
            recipient.outbox_dedupe_key = f"{PREFIX}{campaign.public_id}:user:{recipient.user_id}:{recipient.generation}"
            recipient.status = "pending"
            recipient.started_at = None
            recipient.last_error = None
            recipient.updated_at = now_utc()
            await self._outbox(campaign, recipient)
            retried.append(recipient.id)
        self.session.add(
            CampaignOperation(
                campaign_id=campaign.id,
                kind="retry",
                key_hash=key_hash,
                request_hash=digest("failed-only"),
                recipient_ids=retried,
            )
        )
        if retried:
            campaign.status = "queued"
            campaign.updated_at = now_utc()
        await self.session.flush()
        return await self._dto(campaign)


@dataclass(frozen=True)
class CampaignDeliveryClaim:
    allowed: bool
    status: str
    text: str | None = None
    chat_id: int | None = None
    delay_seconds: float = 0


class CampaignDeliveryService:
    """Persisted no-blind-resend guard used by the existing Telegram listener."""

    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    def _sessions(self):
        return self.session_factory or db.async_session

    async def _locked(self, session, notification_id):
        # Lock campaign before recipient in every path (cancel/retry/claim/finish).
        parts = notification_id.split(":")
        if len(parts) != 5 or parts[0] != "campaign" or parts[2] != "user":
            return None, None
        campaign = await CampaignRepo(session).get(parts[1], lock=True)
        if not campaign:
            return None, None
        recipient = await session.scalar(
            select(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign.id,
                CampaignRecipient.outbox_dedupe_key == notification_id,
            )
            .with_for_update()
        )
        return campaign, recipient

    async def claim(self, notification_id, *, chat_id, text):
        async with self._sessions()() as session, session.begin():
            campaign, recipient = await self._locked(session, notification_id)
            if not recipient:
                return CampaignDeliveryClaim(False, "skipped")
            outbox = await session.get(NotificationOutbox, recipient.outbox_id)
            if recipient.status == "sending":
                # The previous worker disappeared after its durable claim. It may
                # have sent successfully, so automatic delivery is unsafe.
                recipient.status = "unknown"
                recipient.last_error = "interrupted_after_claim"
                if outbox:
                    outbox.status = "unknown"
                await self._complete(session, campaign)
                return CampaignDeliveryClaim(False, "unknown")
            if recipient.status in TERMINAL:
                return CampaignDeliveryClaim(False, recipient.status)
            user = await session.get(User, recipient.user_id)
            if campaign.status == "cancelled":
                recipient.status = "cancelled"
            elif (
                not user
                or user.telegram_delivery_status != "active"
                or user.tg_id != recipient.tg_id_snapshot
                or chat_id != recipient.tg_id_snapshot
                or text != campaign.text
            ):
                recipient.status = "skipped"
                recipient.last_error = "recipient_or_payload_changed"
            elif campaign.status not in {"queued", "running"}:
                return CampaignDeliveryClaim(False, "skipped")
            else:
                due = aware(outbox.next_attempt_at) if outbox else None
                if due and due > now_utc():
                    return CampaignDeliveryClaim(
                        False,
                        "deferred",
                        delay_seconds=(due - now_utc()).total_seconds(),
                    )
                recipient.status = "sending"
                recipient.started_at = recipient.updated_at = now_utc()
                recipient.attempts += 1
                campaign.status = "running"
                if outbox:
                    outbox.status = "sending"
                return CampaignDeliveryClaim(
                    True, "sending", campaign.text, recipient.tg_id_snapshot
                )
            recipient.updated_at = now_utc()
            if outbox:
                outbox.status = recipient.status
            await self._complete(session, campaign)
            return CampaignDeliveryClaim(False, recipient.status)

    async def finish(self, notification_id, *, status, error=None, retry_after=0):
        if status not in {"delivered", "failed", "unknown", "retry"}:
            raise ValueError("Invalid campaign delivery outcome")
        async with self._sessions()() as session, session.begin():
            campaign, recipient = await self._locked(session, notification_id)
            if not recipient or recipient.status != "sending":
                return
            outbox = await session.get(NotificationOutbox, recipient.outbox_id)
            recipient.status = "pending" if status == "retry" else status
            recipient.last_error = error[:96] if error else None
            recipient.updated_at = now_utc()
            if status == "delivered":
                recipient.delivered_at = now_utc()
            if outbox:
                # Retry is driven by the reserved Redis envelope; its visibility
                # heartbeat prevents the PG publisher from an immediate replay.
                outbox.status = "queued" if status == "retry" else status
                outbox.published_at = (
                    now_utc() + timedelta(seconds=max(0, retry_after))
                    if status == "retry"
                    else now_utc()
                )
                outbox.next_attempt_at = (
                    now_utc() + timedelta(seconds=max(0, retry_after))
                    if status == "retry"
                    else now_utc()
                )
                outbox.last_error = recipient.last_error
            await self._complete(session, campaign)

    async def reconcile_stale(self, *, before=None, limit=100):
        """Close abandoned claims even if Redis processing data was lost.

        Reconciliation never republishes or resends. A conservative ten-minute
        default exceeds ordinary Telegram request timeouts and lease turnover.
        """
        before = before or now_utc() - timedelta(
            seconds=max(600, settings.notification_visibility_timeout * 2)
        )
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid reconciliation limit")
        count = 0
        async with self._sessions()() as session, session.begin():
            campaigns = (
                await session.scalars(
                    select(AdminCampaign)
                    .where(
                        select(CampaignRecipient.id)
                        .where(
                            CampaignRecipient.campaign_id == AdminCampaign.id,
                            CampaignRecipient.status == "sending",
                            CampaignRecipient.started_at <= before,
                        )
                        .exists()
                    )
                    .order_by(AdminCampaign.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for campaign in campaigns:
                recipients = (
                    await session.scalars(
                        select(CampaignRecipient)
                        .where(
                            CampaignRecipient.campaign_id == campaign.id,
                            CampaignRecipient.status == "sending",
                            CampaignRecipient.started_at <= before,
                        )
                        .order_by(CampaignRecipient.id)
                        .limit(limit - count)
                        .with_for_update()
                    )
                ).all()
                for recipient in recipients:
                    recipient.status = "unknown"
                    recipient.updated_at = now_utc()
                    recipient.last_error = "stale_delivery_claim"
                    outbox = await session.get(NotificationOutbox, recipient.outbox_id)
                    if outbox:
                        outbox.status = "unknown"
                        outbox.last_error = "stale_delivery_claim"
                    count += 1
                await self._complete(session, campaign)
                if count >= limit:
                    break
        return count

    async def _complete(self, session, campaign):
        await session.flush()
        counts = await CampaignRepo(session).counts(campaign.id)
        if campaign.status != "cancelled" and not any(
            counts[s] for s in ("pending", "queued", "sending")
        ):
            campaign.status = "completed"
        campaign.updated_at = now_utc()
