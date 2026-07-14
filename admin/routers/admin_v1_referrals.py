from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select

from core.db.models import ProviderPayment, ReferralReward, User
from core.db.unit_of_work import uow
from core.services.admin_queries import money, numeric_search_predicates, utc_iso

from ..security import AdminPermission, AdminPrincipal, require_permission

router = APIRouter(prefix="/api/admin/v1/referrals", tags=["admin-v1-referrals"])

ReferralsRead = Annotated[
    AdminPrincipal,
    Depends(require_permission(AdminPermission.REFERRALS_READ)),
]


def _search_condition(q: str | None):
    normalized = (q or "").strip()
    if not normalized:
        return None
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    terms = [User.username.ilike(f"%{escaped}%", escape="\\")]
    terms.extend(
        numeric_search_predicates(
            normalized,
            integer_columns=(User.id,),
            bigint_columns=(User.tg_id,),
        )
    )
    return or_(*terms)


async def _enrich(session, users: list[User]) -> dict[int, dict[str, Any]]:
    ids = [user.id for user in users]
    if not ids:
        return {}
    direct_counts = dict(
        (
            await session.execute(
                select(User.referred_by_id, func.count(User.id))
                .where(User.referred_by_id.in_(ids))
                .group_by(User.referred_by_id)
            )
        ).all()
    )
    deposits = dict(
        (
            await session.execute(
                select(ProviderPayment.user_id, func.sum(ProviderPayment.amount))
                .where(
                    ProviderPayment.user_id.in_(ids),
                    ProviderPayment.status == "credited",
                )
                .group_by(ProviderPayment.user_id)
            )
        ).all()
    )
    rewards = dict(
        (
            await session.execute(
                select(
                    ReferralReward.beneficiary_user_id,
                    func.sum(ReferralReward.reward_amount),
                )
                .where(ReferralReward.beneficiary_user_id.in_(ids))
                .group_by(ReferralReward.beneficiary_user_id)
            )
        ).all()
    )
    return {
        user.id: {
            "user_id": user.id,
            "id": user.id,
            "tg_id": user.tg_id,
            "username": user.username,
            "registered_at": utc_iso(user.created),
            "created_at": utc_iso(user.created),
            "referred_by_id": user.referred_by_id,
            "delivery_status": user.telegram_delivery_status,
            "deposits_total": money(deposits.get(user.id, Decimal("0.00"))),
            "rewards_total": money(rewards.get(user.id, Decimal("0.00"))),
            "direct_referrals": int(direct_counts.get(user.id, 0) or 0),
            "children": [],
        }
        for user in users
    }


@router.get("/tree")
async def referral_tree(
    _principal: ReferralsRead,
    q: str | None = Query(default=None, max_length=128),
    parent_id: int | None = Query(default=None, ge=1),
    max_depth: int = Query(default=2, ge=0, le=10),
    max_nodes: int = Query(default=1_000, ge=1, le=5_000),
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=1_000_000),
):
    """Return a bounded, cycle-safe referral forest.

    The root page remains paginated. Descendants are fetched in batches and a
    hard node budget prevents an unexpectedly broad branch from exhausting an
    admin worker.
    """

    search = _search_condition(q)
    root_conditions = []
    if search is not None:
        root_conditions.append(search)
    elif parent_id is not None:
        root_conditions.append(User.referred_by_id == parent_id)
    else:
        root_conditions.append(User.referred_by_id.is_(None))

    async with uow() as repos:
        session = repos["users"].session
        total = int(
            await session.scalar(
                select(func.count()).select_from(User).where(*root_conditions)
            )
            or 0
        )
        roots = (
            await session.scalars(
                select(User)
                .where(*root_conditions)
                .order_by(User.created.desc(), User.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

        users_by_id = {user.id: user for user in roots}
        child_ids: dict[int, list[int]] = defaultdict(list)
        visited = set(users_by_id)
        frontier = list(users_by_id)
        truncated = False

        for _depth in range(max_depth):
            if not frontier or len(users_by_id) >= max_nodes:
                truncated = bool(frontier) or truncated
                break
            remaining = max_nodes - len(users_by_id)
            children = (
                await session.scalars(
                    select(User)
                    .where(User.referred_by_id.in_(frontier))
                    .order_by(User.created, User.id)
                    .limit(remaining + 1)
                )
            ).all()
            if len(children) > remaining:
                children = children[:remaining]
                truncated = True
            next_frontier = []
            for child in children:
                parent = child.referred_by_id
                if parent is None or child.id in visited:
                    truncated = True
                    continue
                visited.add(child.id)
                users_by_id[child.id] = child
                child_ids[parent].append(child.id)
                next_frontier.append(child.id)
            frontier = next_frontier

        nodes = await _enrich(session, list(users_by_id.values()))

    for parent, children in child_ids.items():
        parent_node = nodes.get(parent)
        if parent_node is None:
            continue
        parent_node["children"] = [nodes[child_id] for child_id in children]

    def assign_level(node: dict[str, Any], level: int, path: set[int]) -> None:
        node_id = int(node["user_id"])
        node["level"] = level
        if node_id in path:
            node["cycle"] = True
            node["children"] = []
            return
        next_path = {*path, node_id}
        for child in node["children"]:
            assign_level(child, level + 1, next_path)

    root_nodes = [nodes[user.id] for user in roots]
    for root in root_nodes:
        assign_level(root, 0, set())

    return {
        "roots": root_nodes,
        "items": root_nodes,
        "total": total,
        "limit": limit,
        "offset": offset,
        "max_depth": max_depth,
        "node_count": len(nodes),
        "truncated": truncated,
    }
