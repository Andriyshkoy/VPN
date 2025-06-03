from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.db import Base


class Server(Base):

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))

    ip: Mapped[str] = mapped_column(String(64))
    port: Mapped[int] = mapped_column(Integer, default=22)

    host: Mapped[str] = mapped_column(String(128))
    location: Mapped[str] = mapped_column(String(128))

    def __repr__(self):
        return f"<Server(name={self.name}, host={self.host}, ip={self.ip})>"
