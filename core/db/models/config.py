from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base

from .server import Server
from .user import User


class VPN_Config(Base):

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)

    server_id: Mapped[int] = mapped_column(
        ForeignKey("server.id", ondelete="CASCADE")
    )
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
        DateTime,
        default=func.now(),
        server_default=func.now()
    )
    suspended: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )

    def __repr__(self):
        return f"<VPN_Config(name={self.name}, server={self.server.name})>"
