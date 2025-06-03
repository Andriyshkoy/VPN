# core/db/models/server.py
from sqlalchemy import Integer, String
from decimal import Decimal
from sqlalchemy import Numeric
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base
from core.db.types.encrypted import EncryptedString


class Server(Base):

    name: Mapped[str] = mapped_column(String(128))

    ip: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer, default=22)

    host: Mapped[str] = mapped_column(String(128))
    monthly_cost: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        default=0
    )
    location: Mapped[str] = mapped_column(String(128))

    api_key: Mapped[str] = mapped_column(EncryptedString)
