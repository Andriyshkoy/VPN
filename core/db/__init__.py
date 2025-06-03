from sqlalchemy import Column, Integer
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base, declared_attr

from core.config import settings

DATABASE_URL = settings.database_url

engine = create_async_engine(
    DATABASE_URL,
    echo=True,
    future=True,
)

async_session = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
)


class PreBase:

    @declared_attr
    def __tablename__(cls):
        return cls.__name__.lower()

    id = Column(Integer, primary_key=True)


Base = declarative_base(cls=PreBase)
