from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update

import core.db as db
from core.db.models import AdminSession

from ..security import (
    CSRF_COOKIE_NAME,
    CSRF_COOKIE_PATH,
    AdminPrincipal,
    AdminPrincipalDependency,
    _aware,
    _digest,
    authenticate_login,
    clear_session_cookies,
    require_permission,
    revoke_session,
    set_session_cookies,
)

router = APIRouter(prefix="/api/admin/v1/auth", tags=["admin-v1-auth"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ActorResponse(BaseModel):
    id: int
    username: str
    role: str
    permissions: list[str]


class AuthResponse(BaseModel):
    actor: ActorResponse
    csrf_token: str


def _set_csrf_cookie(
    response: Response,
    token: str,
    max_age: int,
    *,
    secure: bool | None = None,
) -> None:
    if secure is None:
        from core.config import Settings

        secure = Settings().admin_cookie_secure
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        max_age=max_age,
        path=CSRF_COOKIE_PATH,
        secure=secure,
        httponly=False,
        samesite="strict",
    )


async def _csrf_for_me(
    request: Request, response: Response, principal: AdminPrincipal
) -> str:
    token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if token and secrets.compare_digest(_digest(token), principal.csrf_token_hash):
        return token

    token = secrets.token_urlsafe(32)
    async with db.async_session() as session, session.begin():
        await session.execute(
            update(AdminSession)
            .where(AdminSession.id == principal.session_id)
            .values(csrf_token_hash=_digest(token))
        )
    remaining = max(
        1,
        int(
            (_aware(principal.expires_at) - datetime.now(timezone.utc)).total_seconds()
        ),
    )
    _set_csrf_cookie(response, token, remaining)
    return token


@router.post("/login", response_model=AuthResponse)
async def login(data: LoginRequest, request: Request, response: Response):
    principal, session_token, csrf_token, max_age = await authenticate_login(
        request, username=data.username, password=data.password
    )
    set_session_cookies(
        response,
        session_token=session_token,
        csrf_token=csrf_token,
        max_age=max_age,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"actor": principal.actor_payload(), "csrf_token": csrf_token}


@router.get("/me", response_model=AuthResponse)
async def me(
    request: Request,
    response: Response,
    principal: AdminPrincipalDependency,
):
    csrf_token = await _csrf_for_me(request, response, principal)
    response.headers["Cache-Control"] = "no-store"
    return {"actor": principal.actor_payload(), "csrf_token": csrf_token}


MutationPrincipal = Annotated[AdminPrincipal, Depends(require_permission())]


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    principal: MutationPrincipal,
) -> None:
    await revoke_session(request, principal, all_sessions=False)
    clear_session_cookies(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(
    request: Request,
    response: Response,
    principal: MutationPrincipal,
) -> None:
    await revoke_session(request, principal, all_sessions=True)
    clear_session_cookies(response)
