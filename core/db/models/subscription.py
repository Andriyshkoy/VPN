"""Product subscriptions and their replaceable technical access pointers."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class Subscription(Base):
    __tablename__ = "subscription"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_subscription_user_id"),
        CheckConstraint("plan_code IN ('basic', 'plus')", name="ck_subscription_plan"),
        CheckConstraint(
            "status IN ('pending', 'active', 'suspended', 'expired', 'cancelled')",
            name="ck_subscription_status",
        ),
        CheckConstraint("version >= 1", name="ck_subscription_positive_version"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    migration_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    migration_snapshot: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default=text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )


class SubscriptionAccess(Base):
    __tablename__ = "subscription_access"
    __table_args__ = (
        UniqueConstraint(
            "subscription_id", "service_id", name="uq_subscription_access_service"
        ),
        UniqueConstraint("config_id", name="uq_subscription_access_config_id"),
        CheckConstraint(
            "service_id IN ('hysteria', 'vless-reality')",
            name="ck_subscription_access_service",
        ),
        CheckConstraint("generation >= 1", name="ck_subscription_access_generation"),
    )

    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("subscription.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    service_id: Mapped[str] = mapped_column(String(32), nullable=False)
    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_config.id", ondelete="SET NULL"), nullable=True
    )
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    provision_operation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )
