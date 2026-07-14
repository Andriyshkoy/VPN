from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Annotated, Callable
from urllib.parse import urlsplit

import bcrypt
from anyio import to_thread
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import core.db as db
from core.config import Settings
from core.db.models import AdminAuditEvent, AdminRole, AdminSession, AdminUser

SESSION_COOKIE_NAME = "vpn_admin_session"
CSRF_COOKIE_NAME = "vpn_admin_csrf"
SESSION_COOKIE_PATH = "/api/admin/v1"
# The double-submit value must be readable by the SPA while it is rendered at
# routes such as `/users` and `/servers`. A cookie scoped to the API path is
# sent to mutations but is intentionally hidden from `document.cookie` there.
CSRF_COOKIE_PATH = "/"
# Backwards-compatible name for integrations which scoped the opaque session.
COOKIE_PATH = SESSION_COOKIE_PATH
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DUMMY_PASSWORD_HASH = "$2b$12$iMgCQ8L7TGud/xCKh5JM4utE2pBRBOq0Der4JXWz/Nlf9.5u3FBEO"


class AdminPermission(StrEnum):
    DASHBOARD_READ = "dashboard:read"
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    BALANCE_READ = "balance:read"
    BALANCE_WRITE = "balance:write"
    REFERRALS_READ = "referrals:read"
    FINANCE_READ = "finance:read"
    CONFIGS_READ = "configs:read"
    CONFIGS_WRITE = "configs:write"
    SERVERS_READ = "servers:read"
    SERVERS_WRITE = "servers:write"
    METRICS_READ = "metrics:read"
    AUDIT_READ = "audit:read"
    ADMINS_MANAGE = "admins:manage"


_ALL_PERMISSIONS = frozenset(AdminPermission)
ROLE_PERMISSIONS: dict[AdminRole, frozenset[AdminPermission]] = {
    AdminRole.OWNER: _ALL_PERMISSIONS,
    AdminRole.SUPPORT: frozenset(
        {
            AdminPermission.DASHBOARD_READ,
            AdminPermission.USERS_READ,
            AdminPermission.USERS_WRITE,
            AdminPermission.BALANCE_READ,
            AdminPermission.REFERRALS_READ,
            AdminPermission.CONFIGS_READ,
            AdminPermission.CONFIGS_WRITE,
            AdminPermission.SERVERS_READ,
        }
    ),
    AdminRole.FINANCE: frozenset(
        {
            AdminPermission.DASHBOARD_READ,
            AdminPermission.USERS_READ,
            AdminPermission.BALANCE_READ,
            AdminPermission.BALANCE_WRITE,
            AdminPermission.REFERRALS_READ,
            AdminPermission.FINANCE_READ,
            AdminPermission.CONFIGS_READ,
            AdminPermission.AUDIT_READ,
        }
    ),
    AdminRole.OPS: frozenset(
        {
            AdminPermission.DASHBOARD_READ,
            AdminPermission.USERS_READ,
            AdminPermission.CONFIGS_READ,
            AdminPermission.CONFIGS_WRITE,
            AdminPermission.SERVERS_READ,
            AdminPermission.SERVERS_WRITE,
            AdminPermission.METRICS_READ,
            AdminPermission.AUDIT_READ,
        }
    ),
    AdminRole.VIEWER: frozenset(
        {
            AdminPermission.DASHBOARD_READ,
            AdminPermission.USERS_READ,
            AdminPermission.BALANCE_READ,
            AdminPermission.REFERRALS_READ,
            AdminPermission.FINANCE_READ,
            AdminPermission.CONFIGS_READ,
            AdminPermission.SERVERS_READ,
            AdminPermission.METRICS_READ,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    user_id: int
    username: str
    role: AdminRole
    session_id: int
    csrf_token_hash: str
    expires_at: datetime

    @property
    def permissions(self) -> frozenset[AdminPermission]:
        return ROLE_PERMISSIONS[self.role]

    def actor_payload(self) -> dict[str, object]:
        return {
            "id": self.user_id,
            "username": self.username,
            "role": self.role.value,
            "permissions": sorted(permission.value for permission in self.permissions),
        }


def permissions_for_role(role: AdminRole | str) -> frozenset[AdminPermission]:
    try:
        return ROLE_PERMISSIONS[AdminRole(role)]
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unknown admin role: {role}") from exc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalize_username(value: str) -> str:
    return value.strip().casefold()


def _trusted_proxy_networks(settings: Settings):
    networks = []
    for value in settings.admin_trusted_proxy_cidrs.split(","):
        normalized = value.strip()
        if not normalized:
            continue
        try:
            networks.append(ipaddress.ip_network(normalized, strict=False))
        except ValueError:
            # Invalid operator configuration must never make arbitrary proxy
            # headers trusted. Startup validation can be tightened later.
            continue
    return tuple(networks)


def _client_ip(request: Request, settings: Settings | None = None) -> str:
    peer = request.client.host if request.client else ""
    if not peer:
        return "unknown"
    settings = settings or Settings()
    try:
        current = ipaddress.ip_address(peer)
    except ValueError:
        return "unknown"
    networks = _trusted_proxy_networks(settings)
    if not any(current in network for network in networks):
        return current.compressed

    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [item.strip() for item in forwarded.split(",") if item.strip()]
    if not hops or len(hops) > 10:
        return current.compressed
    # Nginx appends the address it actually accepted to the right. Walk the
    # chain backwards through explicitly trusted proxies and stop at the first
    # untrusted address; attacker-supplied values further left are ignored.
    for raw_hop in reversed(hops):
        try:
            hop = ipaddress.ip_address(raw_hop)
        except ValueError:
            return current.compressed
        if not any(current in network for network in networks):
            break
        current = hop
    return current.compressed


def _client_ip_hash(request: Request, settings: Settings | None = None) -> str:
    settings = settings or Settings()
    return hmac.new(
        settings.encryption_key.encode("utf-8"),
        _client_ip(request, settings).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent", "").strip()
    return value[:512] or None


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or secrets.token_hex(16)


def _correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "") or _request_id(request)


def add_audit_event(
    session: AsyncSession,
    request: Request,
    *,
    action: str,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | int | None = None,
    details: dict | None = None,
) -> AdminAuditEvent:
    """Stage an immutable audit event in the caller's transaction."""

    event = AdminAuditEvent(
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_id=_request_id(request),
        correlation_id=_correlation_id(request),
        client_ip_hash=_client_ip_hash(request),
        user_agent=_user_agent(request),
        details=details or {},
    )
    session.add(event)
    return event


class LoginRateLimiter:
    """Small per-process IP limiter layered on top of DB account lockout.

    Account lock state is shared by every worker. This limiter additionally
    slows username enumeration and password spraying before bcrypt/DB work.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def retry_after(self, key: str, *, limit: int, window: int) -> int:
        now = time.monotonic()
        async with self._lock:
            attempts = self._failures[key]
            while attempts and attempts[0] <= now - window:
                attempts.popleft()
            if len(attempts) < limit:
                return 0
            return max(1, int(window - (now - attempts[0])))

    async def failure(self, key: str) -> None:
        async with self._lock:
            self._failures[key].append(time.monotonic())

    async def success(self, key: str) -> None:
        async with self._lock:
            self._failures.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._failures.clear()


login_rate_limiter = LoginRateLimiter()


async def _password_matches(password: str, password_hash: str) -> bool:
    def check() -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode())
        except (TypeError, ValueError):
            return False

    return await to_thread.run_sync(check)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        # Non-browser clients do not send Origin. Authenticated mutations still
        # require the unguessable CSRF header and matching cookie below.
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
    expected_scheme = forwarded_proto.strip() or request.url.scheme
    return parsed.scheme == expected_scheme and parsed.netloc == request.headers.get(
        "host", request.url.netloc
    )


def require_same_origin(request: Request) -> None:
    if not _same_origin(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin admin request rejected",
        )


def set_session_cookies(
    response: Response,
    *,
    session_token: str,
    csrf_token: str,
    max_age: int,
    secure: bool | None = None,
) -> None:
    if secure is None:
        secure = Settings().admin_cookie_secure
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=max_age,
        path=SESSION_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    # This double-submit token is intentionally readable by same-origin JS;
    # the opaque session itself remains HttpOnly.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=max_age,
        path=CSRF_COOKIE_PATH,
        secure=secure,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response, *, secure: bool | None = None) -> None:
    if secure is None:
        secure = Settings().admin_cookie_secure
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path=CSRF_COOKIE_PATH,
        secure=secure,
        httponly=False,
        samesite="strict",
    )


async def authenticate_login(
    request: Request, *, username: str, password: str
) -> tuple[AdminPrincipal, str, str, int]:
    """Authenticate and create a DB session.

    A successful match against the legacy environment credentials creates the
    first persisted owner. No password or hash is embedded in schema history.
    """

    require_same_origin(request)
    settings = Settings()
    normalized_username = _normalize_username(username)
    rate_key = _client_ip(request)
    retry_after = await login_rate_limiter.retry_after(
        rate_key,
        limit=settings.admin_login_rate_limit_attempts,
        window=settings.admin_login_rate_limit_window_seconds,
    )
    if retry_after:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(retry_after)},
        )

    now = _utcnow()
    async with db.async_session() as session:
        user = await session.scalar(
            select(AdminUser)
            .where(AdminUser.username == normalized_username)
            .with_for_update()
        )

        if user is not None and user.locked_until is not None:
            locked_until = _aware(user.locked_until)
            if locked_until > now:
                await login_rate_limiter.failure(rate_key)
                add_audit_event(
                    session,
                    request,
                    action="admin.login_blocked",
                    actor_user_id=user.id,
                    target_type="admin_user",
                    target_id=user.id,
                    details={"reason": "account_locked"},
                )
                await session.commit()
                seconds = max(1, int((locked_until - now).total_seconds()))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Account temporarily locked",
                    headers={"Retry-After": str(seconds)},
                )

        verified = False
        bootstrapped = False
        if user is not None and user.is_active:
            verified = await _password_matches(password, user.password_hash)
        elif user is None:
            legacy_username = _normalize_username(settings.admin_username)
            legacy_candidate = bool(
                legacy_username
                and settings.admin_password_hash
                and secrets.compare_digest(normalized_username, legacy_username)
            )
            candidate_hash = (
                settings.admin_password_hash
                if legacy_candidate
                else _DUMMY_PASSWORD_HASH
            )
            candidate_verified = await _password_matches(password, candidate_hash)
            verified = legacy_candidate and candidate_verified
            if verified:
                user = AdminUser(
                    username=normalized_username,
                    password_hash=settings.admin_password_hash,
                    role=AdminRole.OWNER.value,
                    is_active=True,
                    failed_login_attempts=0,
                    password_changed_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(user)
                await session.flush()
                bootstrapped = True
        else:
            # Keep disabled and unknown accounts reasonably close in timing.
            await _password_matches(password, _DUMMY_PASSWORD_HASH)

        if not verified or user is None:
            await login_rate_limiter.failure(rate_key)
            if user is not None:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= settings.admin_login_max_failures:
                    user.locked_until = now + timedelta(
                        seconds=settings.admin_login_lockout_seconds
                    )
                user.updated_at = now
            add_audit_event(
                session,
                request,
                action="admin.login_failed",
                actor_user_id=user.id if user is not None else None,
                target_type="admin_user",
                target_id=user.id if user is not None else normalized_username,
                details={"reason": "invalid_credentials"},
            )
            await session.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )

        await login_rate_limiter.success(rate_key)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        user.updated_at = now

        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=settings.admin_session_ttl_seconds)
        admin_session = AdminSession(
            user_id=user.id,
            token_hash=_digest(session_token),
            csrf_token_hash=_digest(csrf_token),
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
            client_ip_hash=_client_ip_hash(request, settings),
            user_agent=_user_agent(request),
        )
        session.add(admin_session)
        await session.flush()
        add_audit_event(
            session,
            request,
            action="admin.login_succeeded",
            actor_user_id=user.id,
            target_type="admin_session",
            target_id=admin_session.id,
            details={"legacy_owner_bootstrap": bootstrapped},
        )
        principal = AdminPrincipal(
            user_id=user.id,
            username=user.username,
            role=AdminRole(user.role),
            session_id=admin_session.id,
            csrf_token_hash=admin_session.csrf_token_hash,
            expires_at=expires_at,
        )
        await session.commit()

    return principal, session_token, csrf_token, settings.admin_session_ttl_seconds


async def get_admin_principal(request: Request) -> AdminPrincipal:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    now = _utcnow()
    async with db.async_session() as session, session.begin():
        row = (
            await session.execute(
                select(AdminSession, AdminUser)
                .join(AdminUser, AdminUser.id == AdminSession.user_id)
                .where(AdminSession.token_hash == _digest(token))
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
            )
        admin_session, user = row
        if (
            admin_session.revoked_at is not None
            or _aware(admin_session.expires_at) <= now
            or not user.is_active
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired or revoked",
            )
        if (now - _aware(admin_session.last_seen_at)).total_seconds() >= 60:
            admin_session.last_seen_at = now

        try:
            role = AdminRole(user.role)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid administrator role",
            ) from exc
        return AdminPrincipal(
            user_id=user.id,
            username=user.username,
            role=role,
            session_id=admin_session.id,
            csrf_token_hash=admin_session.csrf_token_hash,
            expires_at=_aware(admin_session.expires_at),
        )


def validate_csrf(request: Request, principal: AdminPrincipal) -> None:
    require_same_origin(request)
    header_token = request.headers.get("X-CSRF-Token", "")
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not header_token or not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed"
        )
    if not secrets.compare_digest(
        header_token, cookie_token
    ) or not secrets.compare_digest(_digest(header_token), principal.csrf_token_hash):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed"
        )


AdminPrincipalDependency = Annotated[AdminPrincipal, Depends(get_admin_principal)]


def require_permission(
    *permissions: AdminPermission,
) -> Callable[..., AdminPrincipal]:
    """FastAPI dependency enforcing authentication, CSRF, and every permission."""

    required = frozenset(permissions)

    async def dependency(
        request: Request,
        principal: AdminPrincipalDependency,
    ) -> AdminPrincipal:
        if request.method.upper() in UNSAFE_METHODS:
            validate_csrf(request, principal)
        if not required.issubset(principal.permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return principal

    return dependency


def require_any_permission(
    *permissions: AdminPermission,
) -> Callable[..., AdminPrincipal]:
    """Require authentication and at least one of the supplied permissions."""

    required = frozenset(permissions)

    async def dependency(
        request: Request,
        principal: AdminPrincipalDependency,
    ) -> AdminPrincipal:
        if request.method.upper() in UNSAFE_METHODS:
            validate_csrf(request, principal)
        if required and required.isdisjoint(principal.permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
        return principal

    return dependency


async def revoke_session(
    request: Request, principal: AdminPrincipal, *, all_sessions: bool
) -> None:
    now = _utcnow()
    async with db.async_session() as session, session.begin():
        statement = update(AdminSession).where(
            AdminSession.user_id == principal.user_id,
            AdminSession.revoked_at.is_(None),
        )
        if not all_sessions:
            statement = statement.where(AdminSession.id == principal.session_id)
        result = await session.execute(statement.values(revoked_at=now))
        add_audit_event(
            session,
            request,
            action="admin.logout_all" if all_sessions else "admin.logout",
            actor_user_id=principal.user_id,
            target_type="admin_session",
            target_id=principal.session_id,
            details={"revoked_sessions": result.rowcount or 0},
        )
