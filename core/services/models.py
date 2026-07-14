from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Server:
    id: int
    name: str
    ip: str
    port: int
    host: str
    monthly_cost: Decimal
    location: str
    api_key: str
    lifecycle_state: str = "active"
    accepts_new_configs: bool = True
    max_configs: int | None = None
    capacity_reserve: int = 0
    placement_weight: Decimal = Decimal("1")
    provider: str | None = None
    public_endpoint: str | None = None
    manager_instance_id: str | None = None
    version: int = 1
    updated_at: datetime | None = None

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            name=obj.name,
            ip=obj.ip,
            port=obj.port,
            host=obj.host,
            monthly_cost=obj.monthly_cost,
            location=obj.location,
            api_key=obj.api_key,
            lifecycle_state=getattr(obj, "lifecycle_state", "active"),
            accepts_new_configs=getattr(obj, "accepts_new_configs", True),
            max_configs=getattr(obj, "max_configs", None),
            capacity_reserve=getattr(obj, "capacity_reserve", 0),
            placement_weight=getattr(obj, "placement_weight", Decimal("1")),
            provider=getattr(obj, "provider", None),
            public_endpoint=getattr(obj, "public_endpoint", None),
            manager_instance_id=getattr(obj, "manager_instance_id", None),
            version=getattr(obj, "version", 1),
            updated_at=getattr(obj, "updated_at", None),
        )


@dataclass(frozen=True)
class User:
    id: int
    tg_id: int
    username: str | None
    created: datetime
    balance: Decimal
    referral_code: str

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            tg_id=obj.tg_id,
            username=obj.username or "Unknown",
            created=obj.created,
            balance=obj.balance,
            referral_code=obj.referral_code,
        )


@dataclass(frozen=True)
class Config:
    id: int
    name: str
    server_id: int
    owner_id: int
    display_name: str
    created_at: datetime
    suspended: bool
    suspended_at: datetime | None
    desired_state: str = "active"
    actual_state: str = "active"
    operation_id: str | None = None
    last_error: str | None = None
    operation_status: str | None = None
    operation_attempts: int = 0

    @classmethod
    def from_orm(cls, obj, *, operation=None):
        return cls(
            id=obj.id,
            name=obj.name,
            server_id=obj.server_id,
            owner_id=obj.owner_id,
            display_name=obj.display_name,
            created_at=obj.created_at,
            suspended=obj.suspended,
            suspended_at=obj.suspended_at,
            desired_state=getattr(obj, "desired_state", "active"),
            actual_state=getattr(obj, "actual_state", "active"),
            operation_id=getattr(obj, "operation_id", None),
            last_error=getattr(obj, "last_error", None),
            operation_status=(
                getattr(operation, "status", None) if operation is not None else None
            ),
            operation_attempts=(
                int(getattr(operation, "attempts", 0)) if operation is not None else 0
            ),
        )
