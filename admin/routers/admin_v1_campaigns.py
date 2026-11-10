"""Human-reviewed campaigns; launch only stages a durable recipient snapshot."""

from __future__ import annotations

import hashlib
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

import core.db as db
from core.services.campaigns import (
    CampaignConflict,
    CampaignNotFound,
    CampaignService,
    CampaignValidationError,
)

from ..schemas_product import (
    CampaignDraftRequest,
    CampaignLaunchRequest,
    CampaignUpdateRequest,
    ReasonRequest,
)
from ..security import (
    AdminPermission,
    AdminPrincipal,
    add_audit_event,
    require_permission,
)

router = APIRouter(prefix="/api/admin/v1/campaigns", tags=["admin-v1-campaigns"])
CampaignRead = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.CAMPAIGNS_READ))
]
CampaignWrite = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.CAMPAIGNS_WRITE))
]
CampaignSend = Annotated[
    AdminPrincipal, Depends(require_permission(AdminPermission.CAMPAIGNS_SEND))
]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=8, max_length=160)
]


def _raise_campaign_error(exc):
    if isinstance(exc, CampaignNotFound):
        code = 404
    elif isinstance(exc, CampaignConflict):
        code = 409
    else:
        code = 400
    raise HTTPException(
        code, detail={"code": exc.code, "message": exc.message}
    ) from exc


def _audit(session, request, principal, action, campaign, *, reason=None, key=None):
    details = {
        "revision": campaign["revision"],
        "status": campaign["status"],
        "audience_kind": campaign["audience"]["kind"],
        "text_sha256": hashlib.sha256(campaign["text"].encode()).hexdigest(),
        "text_length": len(campaign["text"]),
        "recipient_count": campaign["counts"]["total"],
    }
    if reason is not None:
        details["reason"] = reason
    if key is not None:
        details["idempotency_key_hash"] = hashlib.sha256(key.encode()).hexdigest()
    add_audit_event(
        session,
        request,
        action=action,
        actor_user_id=principal.user_id,
        target_type="campaign",
        target_id=campaign["id"],
        details=details,
    )


@router.get("")
async def list_campaigns(
    _principal: CampaignRead,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    async with db.async_session() as session:
        return await CampaignService(session).list(limit=limit, offset=offset)


@router.post("", status_code=201)
async def create_campaign(
    data: CampaignDraftRequest, request: Request, principal: CampaignWrite
):
    try:
        async with db.async_session() as session, session.begin():
            campaign = await CampaignService(session).create(
                name=data.name,
                text=data.text,
                audience=data.audience.model_dump(),
                actor_user_id=principal.user_id,
            )
            _audit(session, request, principal, "campaign.draft_created", campaign)
            return campaign
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: UUID, _principal: CampaignRead):
    try:
        async with db.async_session() as session:
            return await CampaignService(session).get(str(campaign_id))
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: UUID,
    data: CampaignUpdateRequest,
    request: Request,
    principal: CampaignWrite,
):
    try:
        async with db.async_session() as session, session.begin():
            campaign = await CampaignService(session).update(
                str(campaign_id),
                revision=data.revision,
                name=data.name,
                text=data.text,
                audience=data.audience.model_dump(),
            )
            _audit(session, request, principal, "campaign.draft_updated", campaign)
            return campaign
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.post("/{campaign_id}/preview")
async def preview_campaign(campaign_id: UUID, _principal: CampaignWrite):
    try:
        async with db.async_session() as session, session.begin():
            return await CampaignService(session).preview(str(campaign_id))
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.post("/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: UUID,
    data: CampaignLaunchRequest,
    request: Request,
    principal: CampaignSend,
    idempotency_key: IdempotencyKey,
):
    try:
        async with db.async_session() as session, session.begin():
            campaign = await CampaignService(session).launch(
                str(campaign_id),
                revision=data.revision,
                preview_token=data.preview_token,
                expected_recipient_count=data.expected_recipient_count,
                idempotency_key=idempotency_key,
            )
            _audit(
                session,
                request,
                principal,
                "campaign.launched",
                campaign,
                reason=data.reason,
                key=idempotency_key,
            )
            return campaign
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.post("/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: UUID,
    data: ReasonRequest,
    request: Request,
    principal: CampaignSend,
    idempotency_key: IdempotencyKey,
):
    try:
        async with db.async_session() as session, session.begin():
            campaign = await CampaignService(session).cancel(
                str(campaign_id), idempotency_key=idempotency_key
            )
            _audit(
                session,
                request,
                principal,
                "campaign.cancelled",
                campaign,
                reason=data.reason,
                key=idempotency_key,
            )
            return campaign
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)


@router.post("/{campaign_id}/retry")
async def retry_campaign(
    campaign_id: UUID,
    data: ReasonRequest,
    request: Request,
    principal: CampaignSend,
    idempotency_key: IdempotencyKey,
):
    try:
        async with db.async_session() as session, session.begin():
            campaign = await CampaignService(session).retry_failed(
                str(campaign_id), idempotency_key=idempotency_key
            )
            _audit(
                session,
                request,
                principal,
                "campaign.retry_requested",
                campaign,
                reason=data.reason,
                key=idempotency_key,
            )
            return campaign
    except (CampaignConflict, CampaignNotFound, CampaignValidationError) as exc:
        _raise_campaign_error(exc)
