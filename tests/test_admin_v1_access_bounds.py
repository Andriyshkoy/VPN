from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import or_, select
from sqlalchemy.dialects import postgresql

from admin.routers.admin_v1_configs import router as configs_router
from admin.routers.admin_v1_finance import router as finance_router
from admin.routers.admin_v1_referrals import router as referrals_router
from admin.routers.admin_v1_system import router as system_router
from admin.routers.admin_v1_users import router as users_router
from admin.security import (
    ROLE_PERMISSIONS,
    AdminPermission,
    AdminPrincipal,
    get_admin_principal,
)
from core.db.models import AdminAction, AdminRole, AdminUser, User, VPNOperation
from core.services.admin_queries import numeric_search_predicates

NOW = datetime(2026, 7, 14, 6, 0, tzinfo=timezone.utc)


async def _principal(sessionmaker, role: AdminRole) -> AdminPrincipal:
    async with sessionmaker() as session, session.begin():
        actor = AdminUser(
            username=f"bounds-{role.value}",
            password_hash="$2b$12$unused-but-never-authenticated",
            role=role.value,
        )
        session.add(actor)
        await session.flush()
    return AdminPrincipal(
        user_id=actor.id,
        username=actor.username,
        role=role,
        session_id=1,
        csrf_token_hash=hashlib.sha256(b"unused").hexdigest(),
        expires_at=NOW + timedelta(hours=1),
    )


def _app(principal: AdminPrincipal, *routers) -> FastAPI:
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    app.dependency_overrides[get_admin_principal] = lambda: principal
    return app


def test_numeric_search_predicates_compile_with_postgres_type_bounds():
    within_integer = numeric_search_predicates(
        "2147483647",
        integer_columns=(User.id,),
        bigint_columns=(User.tg_id,),
    )
    assert {predicate.left.name for predicate in within_integer} == {"id", "tg_id"}

    bigint_only = numeric_search_predicates(
        "2147483648",
        integer_columns=(User.id,),
        bigint_columns=(User.tg_id,),
    )
    assert len(bigint_only) == 1
    statement = select(User.id).where(or_(*bigint_only))
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert '"user".tg_id = 2147483648' in sql
    assert '"user".id = 2147483648' not in sql

    bigint_max = numeric_search_predicates(
        "9223372036854775807",
        integer_columns=(User.id,),
        bigint_columns=(User.tg_id,),
    )
    assert len(bigint_max) == 1
    assert bigint_max[0].left.name == "tg_id"
    assert bigint_max[0].right.value == 9_223_372_036_854_775_807

    assert not numeric_search_predicates(
        "9223372036854775808",
        integer_columns=(User.id,),
        bigint_columns=(User.tg_id,),
    )


@pytest.mark.asyncio
async def test_operations_only_return_sources_allowed_by_rbac(
    sessionmaker, monkeypatch
):
    principal = await _principal(sessionmaker, AdminRole.FINANCE)
    async with sessionmaker() as session, session.begin():
        session.add_all(
            [
                VPNOperation(
                    operation_id="rbac-vpn-operation",
                    config_name="rbac-config",
                    kind="provision",
                    payload={},
                    status="succeeded",
                ),
                AdminAction(
                    actor_user_id=principal.user_id,
                    kind="refresh_status",
                    status="succeeded",
                    idempotency_key_hash="a" * 64,
                    request_hash="b" * 64,
                    reason="RBAC source projection test",
                    payload={},
                    result={},
                    started_at=NOW,
                    completed_at=NOW,
                ),
            ]
        )

    app = _app(principal, system_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        implicit = await client.get("/api/admin/v1/operations")
        explicit_vpn = await client.get(
            "/api/admin/v1/operations", params={"source": "vpn"}
        )
        forbidden_server = await client.get(
            "/api/admin/v1/operations", params={"source": "server"}
        )

        assert implicit.status_code == explicit_vpn.status_code == 200
        assert {item["source"] for item in implicit.json()["items"]} == {"vpn"}
        assert {item["source"] for item in explicit_vpn.json()["items"]} == {"vpn"}
        assert forbidden_server.status_code == 403

        monkeypatch.setitem(
            ROLE_PERMISSIONS,
            AdminRole.FINANCE,
            frozenset({AdminPermission.SERVERS_READ}),
        )
        implicit_server = await client.get("/api/admin/v1/operations")
        explicit_server = await client.get(
            "/api/admin/v1/operations", params={"source": "server"}
        )
        forbidden_vpn = await client.get(
            "/api/admin/v1/operations", params={"source": "vpn"}
        )

    assert implicit_server.status_code == explicit_server.status_code == 200
    assert {item["source"] for item in implicit_server.json()["items"]} == {"server"}
    assert {item["source"] for item in explicit_server.json()["items"]} == {"server"}
    assert forbidden_vpn.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/v1/users",
        "/api/admin/v1/configs",
        "/api/admin/v1/finance/ledger",
        "/api/admin/v1/finance/payments",
        "/api/admin/v1/referrals/tree",
        "/api/admin/v1/audit-events",
    ],
)
@pytest.mark.parametrize("query", ["2147483648", "9223372036854775808"])
async def test_numeric_admin_search_api_handles_values_beyond_integer_bounds(
    sessionmaker, path, query
):
    principal = await _principal(sessionmaker, AdminRole.OWNER)
    app = _app(
        principal,
        users_router,
        configs_router,
        finance_router,
        referrals_router,
        system_router,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://admin.test"
    ) as client:
        response = await client.get(path, params={"q": query})

    assert response.status_code == 200, response.text
