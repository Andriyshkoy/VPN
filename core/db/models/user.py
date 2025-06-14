from __future__ import annotations

from datetime import datetime

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base


class User(Base):

    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now()
    )

    balance: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)

    vpn_configs: Mapped[list["VPN_Config"]] = relationship(  # noqa F821 # type: ignore
        "VPN_Config",
        back_populates="owner",
        cascade="all, delete-orphan",
    )
