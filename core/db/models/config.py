from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from core.domain import VPNState

from .server import Server
from .user import User


class VPN_Config(Base):

    name: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True
    )

    server_id: Mapped[int] = mapped_column(ForeignKey("server.id", ondelete="RESTRICT"))
    server: Mapped[Server] = relationship(
        "Server",
        back_populates="vpn_configs",
    )

    owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    owner: Mapped[User] = relationship(
        "User",
        back_populates="vpn_configs",
    )

    display_name: Mapped[str] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now()
    )
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # ``suspended`` remains for backwards-compatible reads.  The lifecycle
    # fields make remote side effects recoverable and observable.
    desired_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VPNState.ACTIVE.value, index=True
    )
    actual_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default=VPNState.ACTIVE.value, index=True
    )
    operation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self):
        return f"<VPN_Config(name={self.name}, server={self.server.name})>"
