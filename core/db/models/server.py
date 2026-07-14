from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from core.db.types.encrypted import EncryptedString
from core.domain import AdminActionStatus, ServerLifecycleState


class Server(Base):

    __table_args__ = (
        CheckConstraint(
            "lifecycle_state IN ('active', 'draining', 'disabled', 'retired')",
            name="ck_server_lifecycle_state",
        ),
        CheckConstraint(
            "max_configs IS NULL OR max_configs > 0",
            name="ck_server_positive_max_configs",
        ),
        CheckConstraint(
            "capacity_reserve >= 0",
            name="ck_server_nonnegative_capacity_reserve",
        ),
        CheckConstraint(
            "max_configs IS NULL OR capacity_reserve < max_configs",
            name="ck_server_reserve_below_capacity",
        ),
        CheckConstraint(
            "placement_weight > 0",
            name="ck_server_positive_placement_weight",
        ),
        CheckConstraint("version >= 1", name="ck_server_positive_version"),
    )

    name: Mapped[str] = mapped_column(String(128))

    ip: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer, default=22)

    host: Mapped[str] = mapped_column(String(128))
    monthly_cost: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    location: Mapped[str] = mapped_column(String(128))

    api_key: Mapped[str] = mapped_column(EncryptedString)

    # Defaults keep existing installations and legacy insert paths working.
    lifecycle_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=ServerLifecycleState.ACTIVE.value,
        server_default=ServerLifecycleState.ACTIVE.value,
        index=True,
    )
    accepts_new_configs: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )
    max_configs: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capacity_reserve: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    placement_weight: Mapped[Decimal] = mapped_column(
        Numeric(8, 3), nullable=False, default=1, server_default="1"
    )
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    public_endpoint: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_instance_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )

    vpn_configs: Mapped[list["VPN_Config"]] = relationship(  # noqa F821 # type: ignore
        "VPN_Config",
        back_populates="server",
        passive_deletes=True,
    )
    status_history: Mapped[list["VPNServerStatus"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )


class VPNServerStatus(Base):
    """A bounded, non-secret Manager status or inventory snapshot."""

    __tablename__ = "vpn_server_status"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('status', 'inventory')", name="ck_vpn_server_status_kind"
        ),
        Index(
            "ix_vpn_server_status_server_collected",
            "server_id",
            "collected_at",
        ),
    )

    server_id: Mapped[int] = mapped_column(
        ForeignKey("server.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    manager_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    manager_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_instance_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    inventory_revision: Mapped[str | None] = mapped_column(
        String(160), nullable=True, index=True
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )

    server: Mapped[Server] = relationship(back_populates="status_history")


class AdminAction(Base):
    """Durable, idempotent record of an administrator fleet action."""

    __tablename__ = "admin_action"
    __table_args__ = (
        UniqueConstraint(
            "actor_user_id",
            "idempotency_key_hash",
            name="uq_admin_action_actor_idempotency",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_admin_action_status",
        ),
        Index("ix_admin_action_server_created", "server_id", "created_at"),
    )

    action_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: str(uuid.uuid4()),
    )
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("server.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_user.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=AdminActionStatus.PENDING.value,
        server_default=AdminActionStatus.PENDING.value,
        index=True,
    )
    idempotency_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_server_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        index=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
