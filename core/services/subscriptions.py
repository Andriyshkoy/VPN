"""Account subscriptions, separate from their replaceable VPN credentials.

Plans define one hourly account charge in the existing billing engine.
Credential issuance is free. Network operations follow their durable DB intent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import and_, or_, select

from core.config import settings
from core.db.models.config import VPN_Config
from core.db.models.subscription import Subscription, SubscriptionAccess
from core.domain import VPNOperationKind
from core.exceptions import (
    InsufficientBalanceError,
    InvalidOperationError,
    UserNotFoundError,
)

from .config import ConfigService
from .node_policy import normalize_node_policy
from .subscription_types import (
    PLANS,
    PlanView,
    SubscriptionAccessView,
    SubscriptionProfile,
    SubscriptionView,
)


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


class SubscriptionUnavailableError(InvalidOperationError):
    """A safe, finite reason code for UI availability messages."""

    def __init__(self, reason_code: str):
        if reason_code not in {"no_active_node", "session_limit_unavailable"}:
            raise ValueError("Unknown subscription availability reason")
        self.reason_code = reason_code
        super().__init__("VPN subscription service is temporarily unavailable")


class SubscriptionService:
    def __init__(self, uow, *, clock=None, config_service=None):
        self._uow = uow
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._config_service = config_service or ConfigService(uow, clock=self._clock)

    @staticmethod
    def catalog() -> tuple[PlanView, ...]:
        return PLANS

    @staticmethod
    def _plan(code: str) -> PlanView:
        for plan in PLANS:
            if plan.code == code:
                return plan
        raise InvalidOperationError("Unknown subscription plan")

    @staticmethod
    def _key(key: str | None, *, required=False) -> str | None:
        if key is None and not required:
            return None
        if not isinstance(key, str) or not key.strip() or len(key) > 160:
            raise InvalidOperationError("Invalid subscription request key")
        return key.strip()

    async def _user(self, repos, user_id):
        user = await repos["users"].get_for_update(user_id)
        if user is None:
            raise UserNotFoundError(f"User with ID {user_id} not found")
        return user

    @staticmethod
    def _validate_access(row, access, cfg):
        if cfg and (cfg.owner_id != row.user_id or cfg.service_id != access.service_id):
            raise InvalidOperationError("Subscription credential ownership mismatch")

    async def _view(self, repos, row, user=None) -> SubscriptionView:
        user = user or await repos["users"].get(id=row.user_id)
        plan = self._plan(row.plan_code)
        accesses = []
        for access in await repos["subscriptions"].list_accesses(row.id):
            cfg = (
                await repos["configs"].get(id=access.config_id)
                if access.config_id
                else None
            )
            self._validate_access(row, access, cfg)
            binding = (
                getattr(cfg, "__dict__", {}).get("access_binding") if cfg else None
            )
            accesses.append(
                SubscriptionAccessView(
                    service_id=access.service_id,
                    config_id=access.config_id,
                    status=(
                        cfg.desired_state
                        if cfg and cfg.desired_state != "active"
                        else cfg.actual_state if cfg else "pending"
                    ),
                    remote_access_id=getattr(binding, "remote_access_id", None),
                    generation=access.generation,
                )
            )
        unlimited = bool(getattr(user, "unlimited_balance", False))
        return SubscriptionView(
            id=row.id,
            user_id=row.user_id,
            plan_code=row.plan_code,
            status=row.status,
            hourly_rate_rub=plan.hourly_rate_rub,
            monthly_price_rub=plan.monthly_price_rub,
            monthly_estimate_rub=plan.monthly_estimate_rub,
            next_charge_at=(
                (
                    _utc(self._clock()).replace(minute=0, second=0, microsecond=0)
                    + timedelta(hours=1)
                )
                if row.status == "active"
                and not unlimited
                and settings.billing_enabled
                and not settings.maintenance_mode
                else None
            ),
            billing_paused=not settings.billing_enabled or settings.maintenance_mode,
            unlimited_balance=unlimited,
            services=plan.services,
            accesses=tuple(accesses),
            version=row.version,
            max_sessions=plan.max_sessions,
        )

    async def get_current(self, user_id: int) -> SubscriptionView | None:
        async with self._uow() as repos:
            row = await repos["subscriptions"].get_by_user(user_id)
            return await self._view(repos, row) if row else None

    async def get_by_tg_id(self, tg_id: int) -> SubscriptionView | None:
        async with self._uow() as repos:
            user = await repos["users"].get_by_tg_id(tg_id)
            if user is None:
                raise UserNotFoundError("User not found")
            row = await repos["subscriptions"].get_by_user(user.id)
            return await self._view(repos, row, user) if row else None

    async def _transition(self, repos, cfg, target, kind) -> str | None:
        if target == "suspended" and cfg.actual_state == "failed":
            # Failed provision cannot accept SUSPEND. Revoke its uncertain
            # identity instead, keeping a later retry from granting access.
            target, kind = "revoked", VPNOperationKind.REVOKE.value
        operation_id = await self._config_service._plan_transition_locked(
            repos, cfg, desired_state=target, kind=kind, now=_utc(self._clock())
        )
        if operation_id:
            row = await repos["subscriptions"].get_by_user(cfg.owner_id)
            operation = await repos["vpn_operations"].get(operation_id=operation_id)
            if row is not None and operation is not None:
                operation.payload = {**operation.payload, "subscription_id": row.id}
        return operation_id

    @staticmethod
    def _dependency(cfg):
        return {
            "id": cfg.id,
            "backend_type": cfg.server.backend_type,
            "config_name": cfg.name,
            "server_id": cfg.server_id,
            "owner_id": cfg.owner_id,
        }

    async def _ready(self, repos, row):
        accesses = await repos["subscriptions"].list_accesses(row.id)
        allowed = self._plan(row.plan_code).services
        current = set()
        for service in allowed:
            access = next(
                (item for item in accesses if item.service_id == service), None
            )
            cfg = (
                await repos["configs"].get(id=access.config_id)
                if access and access.config_id
                else None
            )
            if not cfg:
                return False
            self._validate_access(row, access, cfg)
            if (
                cfg.desired_state != "active"
                or cfg.actual_state != "active"
                or cfg.access_policy != self._policy(self._plan(row.plan_code), service)
            ):
                return False
            operation = (
                await repos["vpn_operations"].get(operation_id=cfg.operation_id)
                if cfg.operation_id
                else None
            )
            if operation is not None and not (
                operation.status == "succeeded"
                or (operation.status == "superseded" and operation.attempts == 0)
            ):
                return False
            current.add(cfg.id)
        # Old generations and legacy OpenVPN credentials must be retired, not
        # merely hidden from the new product screens.
        old = await repos["configs"].list(owner_id=row.user_id)
        return all(cfg.id in current for cfg in old)

    async def reconcile_entitlement(self, user_id):
        """Finalize a pending grant after durable remote execution/retry."""
        async with self._uow() as repos:
            await self._user(repos, user_id)
            row = await repos["subscriptions"].get_by_user(user_id, for_update=True)
            if (
                row is not None
                and row.status == "pending"
                and await self._ready(repos, row)
            ):
                row.status, row.version = "active", row.version + 1
                row.migration_snapshot = {
                    **row.migration_snapshot,
                    "cutover_state": "complete",
                }
                await repos["subscriptions"].session.flush()
                return True
            return False

    async def reconcile_pending(self, *, limit=100):
        """Recover a lost finalization callback after an already successful op."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise InvalidOperationError("Invalid subscription reconciliation limit")

        def active_service(service_id):
            return (
                select(SubscriptionAccess.id)
                .join(VPN_Config, VPN_Config.id == SubscriptionAccess.config_id)
                .where(
                    SubscriptionAccess.subscription_id == Subscription.id,
                    SubscriptionAccess.service_id == service_id,
                    VPN_Config.desired_state == "active",
                    VPN_Config.actual_state == "active",
                )
                .exists()
            )

        async with self._uow() as repos:
            user_ids = (
                await repos["users"].session.scalars(
                    select(Subscription.user_id)
                    .where(
                        Subscription.status == "pending",
                        active_service("hysteria"),
                        or_(
                            Subscription.plan_code == "basic",
                            and_(
                                Subscription.plan_code == "plus",
                                active_service("vless-reality"),
                            ),
                        ),
                    )
                    .order_by(Subscription.updated_at, Subscription.user_id)
                    .limit(limit)
                )
            ).all()
        completed = 0
        for user_id in user_ids:
            completed += int(await self.reconcile_entitlement(user_id))
        return completed

    async def _retire_incompatible(self, repos, row) -> list[str]:
        plan = self._plan(row.plan_code)
        operations = []
        for access in await repos["subscriptions"].list_accesses(row.id):
            cfg = (
                await repos["configs"].get_for_update(access.config_id)
                if access.config_id
                else None
            )
            self._validate_access(row, access, cfg)
            if not cfg or cfg.desired_state == "revoked":
                continue
            if (
                access.service_id not in plan.services
                or cfg.access_policy != self._policy(plan, access.service_id)
            ):
                op = await self._transition(
                    repos, cfg, "revoked", VPNOperationKind.REVOKE.value
                )
                if op:
                    operations.append(op)
        return operations

    async def select_plan(
        self, user_id: int, plan_code: str, request_key=None, *, expected_version=None
    ) -> SubscriptionView:
        self._plan(plan_code)
        self._key(request_key)
        async with self._uow() as repos:
            user = await self._user(repos, user_id)
            row = await repos["subscriptions"].get_by_user(user_id, for_update=True)
            if expected_version is not None and (
                row is None or row.version != expected_version
            ):
                raise InvalidOperationError("Subscription version changed")
            if row is None:
                old_configs = await repos["configs"].list_owner_for_update(user_id)
                retained = [
                    cfg.id for cfg in old_configs if cfg.desired_state != "revoked"
                ]
                if retained and settings.billing_enabled:
                    raise InvalidOperationError(
                        "Pause billing before legacy credential cutover"
                    )
                row = await repos["subscriptions"].create(
                    user_id,
                    plan_code,
                    migration_snapshot={
                        "legacy_config_ids": retained,
                        "cutover_state": "pending" if retained else "complete",
                    },
                )
            elif row.plan_code != plan_code:
                row.plan_code = plan_code
                # No network here: admin may append audit in an outer DB tx.
                await self._retire_incompatible(repos, row)
                if row.status == "active":
                    row.status = "pending"
                row.version += 1
                await repos["subscriptions"].session.flush()
            return await self._view(repos, row, user)

    @staticmethod
    def _policy(plan, service_id):
        policy = {"traffic_limit_bytes": 0, "expires_at": None, "activation_days": None}
        if service_id == "hysteria":
            policy["max_sessions"] = plan.max_sessions
        return normalize_node_policy(service_id, policy)

    async def _server(self, repos, server_id):
        if server_id is not None:
            server = await repos["servers"].get(id=server_id)
            candidates = [server] if server else []
        else:
            candidates = await repos["servers"].list()
        for server in candidates:
            if (
                server.backend_type == "node-agent"
                and server.lifecycle_state == "active"
                and server.accepts_new_configs
            ):
                return server
        raise SubscriptionUnavailableError("no_active_node")

    async def _preflight(self, user_id, server_id):
        async with self._uow() as repos:
            row = await repos["subscriptions"].get_by_user(user_id)
            if row is None:
                raise InvalidOperationError("Select a subscription plan first")
            if (
                settings.billing_enabled
                and row.migration_snapshot.get("cutover_state") == "pending"
            ):
                mapped = {
                    item.config_id
                    for item in await repos["subscriptions"].list_accesses(row.id)
                }
                if any(
                    cfg.id not in mapped
                    for cfg in await repos["configs"].list(owner_id=user_id)
                ):
                    raise InvalidOperationError(
                        "Pause billing before legacy credential cutover"
                    )
            server = await self._server(repos, server_id)
            snapshot = (row.version, row.plan_code, server.id)
            plan = self._plan(row.plan_code)
            if plan.max_sessions is None:
                return snapshot
            params = (server.ip, server.port, server.api_key, server.node_id)
        async with self._config_service._create_node_gateway(
            *params[:3], node_id=params[3]
        ) as api:
            catalog = await api.catalog()
        hys = next((item for item in catalog if item.id == "hysteria"), None)
        capabilities = hys.capabilities if hys else {}
        if (
            capabilities.get("max_sessions") is not True
            or capabilities.get("session_scope") != "auth_id_per_server_instance"
            or capabilities.get("enforcement") != "atomic_quic_connection"
        ):
            raise SubscriptionUnavailableError("session_limit_unavailable")
        return snapshot

    async def issue(
        self, user_id: int, server_id: int | None = None, request_key=None
    ) -> SubscriptionView:
        return await self._issue(user_id, server_id, self._key(request_key))

    async def regenerate(
        self, user_id: int, service_id: str, request_key
    ) -> SubscriptionView:
        return await self._issue(
            user_id,
            None,
            self._key(request_key, required=True),
            regenerate_service=service_id,
        )

    async def _issue(
        self,
        user_id,
        server_id,
        request_key,
        *,
        regenerate_service=None,
        allow_resume=False,
    ):
        version, selected_plan, server_id = await self._preflight(user_id, server_id)
        planned = []
        async with self._uow() as repos:
            user = await self._user(repos, user_id)
            row = await repos["subscriptions"].get_by_user(user_id, for_update=True)
            if row.version != version or row.plan_code != selected_plan:
                raise InvalidOperationError("Subscription changed; retry the request")
            if row.status == "cancelled" or (
                row.status in {"suspended", "expired"} and not allow_resume
            ):
                raise InvalidOperationError(
                    "Resume the subscription before issuing credentials"
                )
            if (
                settings.billing_enabled
                and not user.unlimited_balance
                and user.balance <= 0
            ):
                raise InsufficientBalanceError(
                    "Top up the balance to activate the subscription"
                )
            plan = self._plan(row.plan_code)
            if (
                regenerate_service is not None
                and regenerate_service not in plan.services
            ):
                raise InvalidOperationError(
                    "Service is not included in the subscription"
                )
            planned.extend(await self._retire_incompatible(repos, row))
            retired_configs = []
            access_rows = await repos["subscriptions"].list_accesses(row.id)
            pointers = {item.config_id for item in access_rows if item.config_id}
            owned = await repos["configs"].list_owner_for_update(user_id)
            if (
                not any(cfg.id not in pointers for cfg in owned)
                and row.migration_snapshot.get("cutover_state") == "pending"
            ):
                row.migration_snapshot = {
                    **row.migration_snapshot,
                    "cutover_state": "complete",
                }
            for old in owned:
                if old.id in pointers and old.desired_state != "revoked":
                    continue
                dependency = self._dependency(old)
                op = (
                    old.operation_id
                    if old.desired_state == "revoked"
                    else await self._transition(
                        repos, old, "revoked", VPNOperationKind.REVOKE.value
                    )
                )
                if op:
                    planned.append(op)
                # Definitively rejected credentials may be deleted locally by
                # the transition planner because none ever existed remotely.
                if await repos["configs"].get(id=old.id) is not None:
                    retired_configs.append(dependency)
            for service in plan.services:
                access = await repos["subscriptions"].ensure_access(row.id, service)
                cfg = (
                    await repos["configs"].get_for_update(access.config_id)
                    if access.config_id
                    else None
                )
                self._validate_access(row, access, cfg)
                operation_id = (
                    str(
                        uuid5(
                            NAMESPACE_URL,
                            f"subscription-reissue:{row.id}:{service}:{request_key}",
                        )
                    )
                    if regenerate_service == service
                    else None
                )
                previous_request = (
                    await repos["vpn_operations"].get(operation_id=operation_id)
                    if operation_id
                    else None
                )
                if previous_request:
                    planned.extend(
                        previous_request.payload.get(
                            "subscription_prior_operations", []
                        )
                    )
                    planned.append(previous_request.operation_id)
                    continue
                rotate = regenerate_service == service or (
                    cfg is not None
                    and (
                        cfg.desired_state == "revoked"
                        or cfg.access_policy != self._policy(plan, service)
                    )
                )
                prior = list(planned)
                prior_configs = list(retired_configs)
                if cfg and rotate:
                    op = (
                        cfg.operation_id
                        if cfg.desired_state == "revoked"
                        else await self._transition(
                            repos, cfg, "revoked", VPNOperationKind.REVOKE.value
                        )
                    )
                    if op:
                        prior.append(op)
                        planned.append(op)
                    if not any(item["id"] == cfg.id for item in prior_configs):
                        prior_configs.append(self._dependency(cfg))
                    access.generation += 1
                if cfg and not rotate:
                    if cfg.desired_state == "suspended":
                        op = await self._transition(
                            repos, cfg, "active", VPNOperationKind.UNSUSPEND.value
                        )
                        if op:
                            planned.append(op)
                    elif cfg.operation_id:
                        planned.append(cfg.operation_id)
                    continue
                if cfg is None and access.provision_operation_id is not None:
                    # The reconciler may have completed a previous revocation
                    # and SET NULL on the pointer before issue is requested.
                    # Never reuse that tombstoned credential's operation ID.
                    access.generation += 1
                operation_id = operation_id or str(
                    uuid5(
                        NAMESPACE_URL,
                        f"subscription-issue:{row.id}:{service}:{access.generation}",
                    )
                )
                context = await self._config_service.prepare_config(
                    repos=repos,
                    operation_id=operation_id,
                    server_id=server_id,
                    owner_id=user_id,
                    name=f"sub_{row.id.replace('-', '')[:20]}_{service[:2]}_{access.generation}",
                    display_name=f"{plan.name} · {service}",
                    service_id=service,
                    access_policy=self._policy(plan, service),
                    operation_payload={
                        "subscription_id": row.id,
                        "subscription_prior_operations": prior,
                        "subscription_prior_configs": prior_configs,
                    },
                )
                access.config_id, access.provision_operation_id = (
                    context.config_id,
                    operation_id,
                )
                planned.append(operation_id)
            if row.status != "pending" and (
                (allow_resume and row.status in {"suspended", "expired"})
                or not await self._ready(repos, row)
            ):
                row.status, row.version = "pending", row.version + 1
            await repos["subscriptions"].session.flush()
        # Ordered revocation (including server-side connection close) precedes
        # creation of a replacement credential on plan downgrade/reissue.
        for operation_id in dict.fromkeys(planned):
            result = await self._config_service._execute(operation_id)
            if result != "succeeded":
                break
        await self.reconcile_entitlement(user_id)
        return await self.get_current(user_id)

    async def suspend(self, user_id: int, request_key=None) -> SubscriptionView:
        self._key(request_key)
        planned = []
        async with self._uow() as repos:
            user = await self._user(repos, user_id)
            row = await repos["subscriptions"].get_by_user(user_id, for_update=True)
            if row is None:
                raise InvalidOperationError("Subscription not found")
            for cfg in await repos["configs"].list_owner_for_update(user_id):
                if cfg.desired_state in {"active", "suspended"}:
                    op = await self._transition(
                        repos, cfg, "suspended", VPNOperationKind.SUSPEND.value
                    )
                    if op:
                        planned.append(op)
            if row.status != "suspended":
                row.status, row.version = "suspended", row.version + 1
            view = await self._view(repos, row, user)
        await self._config_service.execute_operations(planned, owner_id=user_id)
        return view

    async def resume(self, user_id: int, request_key=None) -> SubscriptionView:
        # Capability/server preflight runs before any suspended state changes.
        return await self._issue(
            user_id, None, self._key(request_key), allow_resume=True
        )

    async def export(self, user_id: int, service_id: str) -> SubscriptionProfile:
        async with self._uow() as repos:
            row = await repos["subscriptions"].get_by_user(user_id)
            if (
                row is None
                or row.status != "active"
                or service_id not in self._plan(row.plan_code).services
            ):
                raise InvalidOperationError(
                    "Service is not available for this subscription"
                )
            access = await repos["subscriptions"].get_access(row.id, service_id)
            cfg = (
                await repos["configs"].get(id=access.config_id)
                if access and access.config_id
                else None
            )
            if (
                not cfg
                or cfg.owner_id != user_id
                or cfg.desired_state != "active"
                or cfg.actual_state != "active"
            ):
                raise InvalidOperationError("Credential is not active")
            if cfg.access_policy != self._policy(self._plan(row.plan_code), service_id):
                raise InvalidOperationError(
                    "Reissue the credential for the current plan"
                )
            config_id = cfg.id
        profile = await self._config_service.export_profile(config_id)
        return SubscriptionProfile(
            service_id=service_id,
            uri=profile.uri,
            format=profile.format,
            content=profile.content,
        )

    async def prepare_entitlement(self, *, repos, user_id, desired_state, kind):
        """Suspend all old access, restore only compatible product credentials."""
        row = await repos["subscriptions"].get_by_user(user_id, for_update=True)
        if row is None:
            return []
        operations = []
        if desired_state == "active":
            cfgs = []
            for access in await repos["subscriptions"].list_accesses(row.id):
                cfg = (
                    await repos["configs"].get_for_update(access.config_id)
                    if access.config_id
                    else None
                )
                self._validate_access(row, access, cfg)
                if (
                    cfg
                    and cfg.actual_state != "failed"
                    and access.service_id in self._plan(row.plan_code).services
                    and cfg.access_policy
                    == self._policy(self._plan(row.plan_code), access.service_id)
                ):
                    cfgs.append(cfg)
        else:
            cfgs = await repos["configs"].list_owner_for_update(user_id)
        for cfg in cfgs:
            if cfg.desired_state in {"active", "suspended"}:
                op = await self._transition(repos, cfg, desired_state, kind)
                if op:
                    operations.append(op)
        if desired_state == "suspended" and row.status == "active":
            row.status, row.version = "expired", row.version + 1
        elif desired_state == "active" and row.status == "expired":
            # Reconciler will mark active only after all mapped keys work.
            row.status, row.version = (
                "active" if await self._ready(repos, row) else "pending",
                row.version + 1,
            )
        await repos["subscriptions"].session.flush()
        return operations
