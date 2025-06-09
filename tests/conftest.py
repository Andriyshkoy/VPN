import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Ensure environment variables required by settings are present
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())

import core.db as db
from core.db import Base


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture()
async def engine():
    import core.db.models  # noqa
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
def sessionmaker(engine, monkeypatch):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db.unit_of_work, "async_session", maker, raising=False)
    monkeypatch.setattr(db, "async_session", maker, raising=False)
    monkeypatch.setattr(db, "engine", engine, raising=False)
    return maker


@pytest_asyncio.fixture()
async def session(sessionmaker):
    async with sessionmaker() as session:
        yield session
