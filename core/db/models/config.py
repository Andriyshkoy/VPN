from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from core.db.models import Server, User


class VPN_Config(Base):

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))

    server_id: Mapped[int] = mapped_column(ForeignKey("server.id"))
    server: Mapped[Server] = relationship("Server")

    owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    owner: Mapped[User] = relationship("User")

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=func.now(),
        server_default=func.now()
    )
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True
    )

    def __repr__(self):
        return f"<VPN_Config(name={self.name}, server={self.server.name})>"
