from sqlalchemy import text

import pytest

from core.db.repo import ServerRepo


@pytest.mark.asyncio
async def test_server_api_key_is_encrypted(session):
    repo = ServerRepo(session)
    server = await repo.create(
        name="vpn",
        ip="1.1.1.1",
        port=22,
        host="host",
        location="US",
        api_key="secret",
        cost=1,
    )

    assert server.api_key == "secret"

    result = await session.execute(
        text("select api_key from server where id = :id"),
        {"id": server.id},
    )
    raw = result.scalar_one()
    raw_bytes = bytes(raw)

    assert raw_bytes != b"secret"
