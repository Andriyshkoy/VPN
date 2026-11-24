"""Explicit, offline decommission of paused OpenVPN installations.

Not a startup migration or an HTTP action. Review preview() before apply().
No network calls, money changes, subscription activation or remote revoke claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import delete, or_, select, text, update

from core.db.models import (
    AdminAuditEvent,
    AdminUser,
    Server,
    Subscription,
    SubscriptionAccess,
    VPN_Config,
    VPNAccessBinding,
    VPNOperation,
)
from core.exceptions import InvalidOperationError

ACTION = "openvpn.decommission_completed"
NONTERMINAL = {"pending", "failed", "running"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def snapshot(row, fields):
    result = {}
    for name in fields.split():
        value = getattr(row, name)
        result[name] = value.isoformat() if isinstance(value, datetime) else value
    return result


class OpenVPNRetirement:
    def __init__(self, uow):
        self.uow = uow

    async def _preview(self, session):
        servers = list(
            await session.scalars(
                select(Server)
                .where(Server.backend_type == "openvpn-manager")
                .order_by(Server.id)
            )
        )
        ids = [s.id for s in servers]
        configs = list(
            await session.scalars(
                select(VPN_Config)
                .where(VPN_Config.server_id.in_(ids))
                .order_by(VPN_Config.id)
            )
        )
        owners = sorted({c.owner_id for c in configs})
        config_ids = {c.id for c in configs}
        subscriptions = list(
            await session.scalars(
                select(Subscription)
                .where(Subscription.user_id.in_(owners))
                .order_by(Subscription.user_id)
            )
        )
        operations = list(
            await session.scalars(
                select(VPNOperation)
                .where(VPNOperation.server_id.in_(ids))
                .order_by(VPNOperation.id)
            )
        )
        # Also inspect terminal operations referenced by surviving credentials:
        # the executor checks dependency payloads before its terminal-state fence.
        references = set(await session.scalars(select(VPN_Config.operation_id))) | set(
            await session.scalars(select(SubscriptionAccess.provision_operation_id))
        )
        blockers = []
        revoked = {
            (o.owner_id, o.server_id, o.config_name)
            for o in operations
            if o.kind == "revoke" and o.status == "succeeded"
        }
        for op in await session.scalars(
            select(VPNOperation).where(VPNOperation.server_id.not_in(ids))
        ):
            if (
                op.status not in NONTERMINAL | {"exhausted"}
                and op.operation_id not in references
            ):
                continue
            deps = op.payload.get("subscription_prior_configs", [])
            if not isinstance(deps, list) or any(
                not isinstance(dep, dict)
                or dep.get("id") in config_ids
                or (
                    dep.get("server_id") in ids
                    and (op.owner_id, dep.get("server_id"), dep.get("config_name"))
                    not in revoked
                )
                for dep in deps
            ):
                blockers.append(op.operation_id)
        return {
            "format": 1,
            "servers": [
                snapshot(
                    s,
                    "id backend_type node_id lifecycle_state accepts_new_configs version endpoint_revision",
                )
                for s in servers
            ],
            "configs": [
                snapshot(
                    c,
                    "id name server_id owner_id display_name service_id access_policy created_at suspended suspended_at desired_state actual_state operation_id updated_at",
                )
                for c in configs
            ],
            "subscriptions": [
                snapshot(
                    s,
                    "id user_id plan_code status version migration_source migration_snapshot updated_at",
                )
                for s in subscriptions
            ],
            "operations": [
                dict(
                    snapshot(
                        o,
                        "operation_id config_id config_name server_id owner_id kind status attempts lease_token lease_until updated_at",
                    ),
                    payload_sha256=digest(o.payload),
                )
                for o in operations
            ],
            "node_dependency_blockers": sorted(blockers),
            "legacy_access_links": list(
                await session.scalars(
                    select(SubscriptionAccess.id)
                    .where(SubscriptionAccess.config_id.in_(config_ids))
                    .order_by(SubscriptionAccess.id)
                )
            ),
            "legacy_binding_links": list(
                await session.scalars(
                    select(VPNAccessBinding.id)
                    .where(
                        or_(
                            VPNAccessBinding.config_ref.in_(config_ids),
                            VPNAccessBinding.config_id.in_(config_ids),
                        )
                    )
                    .order_by(VPNAccessBinding.id)
                )
            ),
        }

    async def preview(self):
        async with self.uow() as repos:
            return await self._preview(repos["users"].session)

    async def apply(self, approved, *, batch_id, actor_user_id, reason):
        if not re.fullmatch(r"[a-zA-Z0-9:_-]{1,96}", batch_id):
            raise InvalidOperationError("Invalid decommission batch ID")
        if not isinstance(reason, str) or not 10 <= len(reason) <= 500:
            raise InvalidOperationError("Owner shutdown attestation is required")
        request_hash = digest(approved)
        async with self.uow() as repos:
            session = repos["users"].session
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("SET LOCAL lock_timeout='5s'"))
                await session.execute(text("SET LOCAL statement_timeout='30s'"))
                # A short DB-only transaction prevents new intents or financial
                # actions interleaving with the reviewed snapshot. Busy means abort.
                await session.execute(
                    text(
                        'LOCK TABLE "user", subscription, server, vpn_config, '
                        "vpn_operation, subscription_access, vpn_access_binding, "
                        "admin_audit_event IN SHARE ROW EXCLUSIVE MODE"
                    )
                )
            actor = await session.get(AdminUser, actor_user_id)
            if actor is None or not actor.is_active or actor.role != "owner":
                raise InvalidOperationError("An active owner actor is required")
            previous = await session.scalar(
                select(AdminAuditEvent).where(
                    AdminAuditEvent.action == ACTION,
                    AdminAuditEvent.request_id == batch_id,
                )
            )
            current = await self._preview(session)
            if previous:
                if (
                    previous.details.get("manifest_sha256") != request_hash
                    or current["configs"]
                    or any(
                        s["lifecycle_state"] != "retired" or s["accepts_new_configs"]
                        for s in current["servers"]
                    )
                ):
                    raise InvalidOperationError("Decommission replay does not match")
                return {**previous.details["result"], "replayed": True}
            if current != approved:
                raise InvalidOperationError(
                    "Decommission snapshot changed; review again"
                )
            configs, subs, servers = (
                current[k] for k in ("configs", "subscriptions", "servers")
            )
            if not servers or not configs:
                raise InvalidOperationError("No legacy credentials to decommission")
            owners = {c["owner_id"] for c in configs}
            if {s["user_id"] for s in subs} != owners:
                raise InvalidOperationError("Every legacy owner needs a subscription")
            if any(
                c["desired_state"] != "suspended"
                or c["actual_state"] != "suspended"
                or c["service_id"] is not None
                for c in configs
            ):
                raise InvalidOperationError(
                    "Only paused legacy credentials may be archived"
                )
            if any(
                s["status"] not in {"suspended", "expired", "cancelled"} for s in subs
            ):
                raise InvalidOperationError(
                    "Active legacy subscription needs separate review"
                )
            if any(o["status"] == "running" for o in current["operations"]):
                raise InvalidOperationError("Legacy operation is still running")
            if any(
                current[k]
                for k in (
                    "node_dependency_blockers",
                    "legacy_access_links",
                    "legacy_binding_links",
                )
            ):
                raise InvalidOperationError(
                    "Live legacy dependencies need separate review"
                )
            now = datetime.now(timezone.utc)
            common = {
                "batch_id": batch_id,
                "reason": reason,
                "remote_revocation_verified": False,
                "shutdown_attested_by_owner": True,
            }

            def audit(action, target_type, target_id, details):
                session.add(
                    AdminAuditEvent(
                        actor_user_id=actor_user_id,
                        action=action,
                        target_type=target_type,
                        target_id=str(target_id),
                        request_id=batch_id,
                        correlation_id=batch_id,
                        details={**common, **details},
                    )
                )

            for c in configs:
                audit(
                    "openvpn.credential_archived",
                    "user",
                    c["owner_id"],
                    {
                        "config": c,
                        "operation_ids": [
                            o["operation_id"]
                            for o in current["operations"]
                            if o["config_id"] == c["id"]
                        ],
                    },
                )
            await session.flush()
            server_ids = [s["id"] for s in servers]
            await session.execute(
                update(VPNOperation)
                .where(
                    VPNOperation.server_id.in_(server_ids),
                    VPNOperation.status.in_(("pending", "failed")),
                )
                .values(
                    status="superseded",
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                    completed_at=now,
                )
            )
            await session.execute(
                delete(VPN_Config).where(VPN_Config.id.in_([c["id"] for c in configs]))
            )
            for s in subs:
                row = await session.get(Subscription, s["id"])
                row.migration_snapshot = {
                    **row.migration_snapshot,
                    "cutover_state": "complete",
                    "openvpn_decommission": {
                        **common,
                        "archived_config_ids": [
                            c["id"] for c in configs if c["owner_id"] == row.user_id
                        ],
                    },
                }
                row.version += 1
                audit(
                    "openvpn.subscription_detached",
                    "user",
                    row.user_id,
                    {
                        "subscription_id": row.id,
                        "previous_version": s["version"],
                        "status_preserved": row.status,
                        "plan_preserved": row.plan_code,
                    },
                )
            for s in servers:
                row = await session.get(Server, s["id"])
                row.lifecycle_state, row.accepts_new_configs = "retired", False
                row.version += 1
                audit("openvpn.server_retired", "server", row.id, {"before": s})
            result = {
                "archived_configs": len(configs),
                "accounts_detached": len(owners),
                "servers_retired": server_ids,
                "replayed": False,
            }
            audit(
                ACTION,
                "system",
                "openvpn",
                {"manifest_sha256": request_hash, "result": result},
            )
            await session.flush()
            return result
