"""Durable, non-secret identity of a node-agent access, including after revoke."""

from __future__ import annotations

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class VPNAccessBinding(Base):
    __tablename__ = "vpn_access_binding"
    __table_args__ = (
        UniqueConstraint("config_id", name="uq_vpn_binding_config_id"),
        UniqueConstraint("config_ref", name="uq_vpn_binding_config_ref"),
        UniqueConstraint(
            "provision_operation_id", name="uq_vpn_binding_provision_operation"
        ),
        UniqueConstraint(
            "node_id", "remote_access_id", name="uq_vpn_binding_remote_access"
        ),
        CheckConstraint(
            "service_id IN ('hysteria', 'vless-reality')",
            name="ck_vpn_binding_service_id",
        ),
    )

    config_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_config.id", ondelete="SET NULL"), nullable=True
    )
    # Retained even after the FK becomes NULL; never repurpose a revoked binding.
    config_ref: Mapped[int] = mapped_column(Integer, nullable=False)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("server.id", ondelete="SET NULL"), nullable=True, index=True
    )
    node_id: Mapped[str] = mapped_column(String(63), nullable=False)
    service_id: Mapped[str] = mapped_column(String(32), nullable=False)
    provision_operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    remote_access_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    policy: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    observed_state: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="provisioning",
        server_default="provisioning",
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    config: Mapped["VPN_Config | None"] = relationship(
        "VPN_Config", back_populates="access_binding"
    )
