from types import SimpleNamespace

import pytest

from deploy import manager_smoke


class FakeGateway:
    statuses: dict[int, object] = {}
    inventory_calls: list[int] = []

    def __init__(self, server, **_kwargs):
        self.server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get_status(self):
        return self.statuses[self.server.id]

    async def get_client_inventory(self):
        self.inventory_calls.append(self.server.id)
        return SimpleNamespace(revision="sha256:test", count=0, clients=())


def manager_status(
    instance_id: str,
    *,
    ready: bool = True,
    data_plane: str = "up",
):
    return SimpleNamespace(
        instance_id=instance_id,
        readiness=SimpleNamespace(ready=ready),
        data_plane=SimpleNamespace(status=data_plane),
    )


@pytest.fixture(autouse=True)
def fake_gateway(monkeypatch):
    FakeGateway.statuses = {}
    FakeGateway.inventory_calls = []
    monkeypatch.setattr(manager_smoke, "APIGateway", FakeGateway)


@pytest.mark.asyncio
async def test_manager_smoke_requires_status_and_inventory_contract():
    server = SimpleNamespace(id=1, manager_instance_id=None)
    FakeGateway.statuses[1] = manager_status("56c1ab62-0c42-4f03-83c6-4c8e6c43e29b")

    seen: set[str] = set()
    await manager_smoke.verify_manager(server, seen_instance_ids=seen)

    assert FakeGateway.inventory_calls == [1]
    assert seen == {"56c1ab62-0c42-4f03-83c6-4c8e6c43e29b"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ready", "data_plane", "message"),
    [
        (False, "up", "readiness failed"),
        (True, "unknown", "data plane is not up"),
        (True, "stale", "data plane is not up"),
    ],
)
async def test_manager_smoke_rejects_unhealthy_manager(
    ready: bool,
    data_plane: str,
    message: str,
):
    server = SimpleNamespace(id=7, manager_instance_id=None)
    FakeGateway.statuses[7] = manager_status(
        "56c1ab62-0c42-4f03-83c6-4c8e6c43e29b",
        ready=ready,
        data_plane=data_plane,
    )

    with pytest.raises(RuntimeError, match=message):
        await manager_smoke.verify_manager(server, seen_instance_ids=set())


@pytest.mark.asyncio
async def test_manager_smoke_rejects_identity_mismatch_and_duplicate():
    observed = "56c1ab62-0c42-4f03-83c6-4c8e6c43e29b"
    server = SimpleNamespace(
        id=2,
        manager_instance_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    FakeGateway.statuses[2] = manager_status(observed)

    with pytest.raises(RuntimeError, match="identity changed"):
        await manager_smoke.verify_manager(server, seen_instance_ids=set())

    server.manager_instance_id = observed
    with pytest.raises(RuntimeError, match="Multiple server records"):
        await manager_smoke.verify_manager(server, seen_instance_ids={observed})
