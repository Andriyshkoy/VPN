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
        )


@dataclass(frozen=True)
class User:
    id: int
    tg_id: int
    username: str | None
    created: datetime
    balance: Decimal

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            tg_id=obj.tg_id,
            username=obj.username or 'Unknown',
            created=obj.created,
            balance=obj.balance,
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

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            name=obj.name,
            server_id=obj.server_id,
            owner_id=obj.owner_id,
            display_name=obj.display_name,
            created_at=obj.created_at,
            suspended=obj.suspended,
            suspended_at=obj.suspended_at,
        )


@dataclass(frozen=True)
class BillingSettings:
    id: int
    config_creation_cost: Decimal
    monthly_config_cost: Decimal
    referral_first_deposit_bonus_pct: Decimal
    referral_recurring_bonus_pct: Decimal
    updated_at: datetime

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            config_creation_cost=obj.config_creation_cost,
            monthly_config_cost=obj.monthly_config_cost,
            referral_first_deposit_bonus_pct=obj.referral_first_deposit_bonus_pct,
            referral_recurring_bonus_pct=obj.referral_recurring_bonus_pct,
            updated_at=obj.updated_at,
        )


@dataclass(frozen=True)
class BalanceTransaction:
    id: int
    user_id: int
    related_user_id: int | None
    config_id: int | None
    amount: Decimal
    kind: str
    source: str
    description: str | None
    created_at: datetime

    @classmethod
    def from_orm(cls, obj):
        return cls(
            id=obj.id,
            user_id=obj.user_id,
            related_user_id=obj.related_user_id,
            config_id=obj.config_id,
            amount=obj.amount,
            kind=obj.kind,
            source=obj.source,
            description=obj.description,
            created_at=obj.created_at,
        )
