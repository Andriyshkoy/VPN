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


def install_fleet(monkeypatch, servers):
    class Servers:
        async def list(self):
            return servers

    class UnitOfWork:
        async def __aenter__(self):
            return {"servers": Servers()}

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(manager_smoke, "uow", UnitOfWork)


@pytest.mark.asyncio
async def test_main_checks_manager_and_skips_node_agent(monkeypatch, capsys):
    manager = SimpleNamespace(
        id=11, backend_type="openvpn-manager", manager_instance_id=None
    )
    node = SimpleNamespace(id=12, backend_type="node-agent", node_id="nl-test")
    install_fleet(monkeypatch, [manager, node])
    FakeGateway.statuses[11] = manager_status("manager-11")

    await manager_smoke.main()

    # The node has no Manager fixture. Routing it through the old mTLS gateway
    # would fail rather than accidentally passing against a permissive mock.
    assert FakeGateway.inventory_calls == [11]
    assert capsys.readouterr().out == "manager_smoke=ok\n"


@pytest.mark.asyncio
async def test_main_checks_every_manager_in_mixed_fleet(monkeypatch):
    managers = [
        SimpleNamespace(
            id=server_id,
            backend_type="openvpn-manager",
            manager_instance_id=f"manager-{server_id}",
        )
        for server_id in (21, 22, 23)
    ]
    node = SimpleNamespace(id=24, backend_type="node-agent")
    install_fleet(monkeypatch, [managers[0], node, *managers[1:]])
    FakeGateway.statuses = {
        manager.id: manager_status(manager.manager_instance_id) for manager in managers
    }

    await manager_smoke.main()

    assert FakeGateway.inventory_calls == [21, 22, 23]


@pytest.mark.asyncio
async def test_main_shares_manager_identity_history_across_filtered_nodes(monkeypatch):
    managers = [
        SimpleNamespace(id=server_id, backend_type="openvpn-manager")
        for server_id in (31, 32)
    ]
    install_fleet(
        monkeypatch,
        [managers[0], SimpleNamespace(id=33, backend_type="node-agent"), managers[1]],
    )
    FakeGateway.statuses = {
        manager.id: manager_status("duplicate-manager-instance") for manager in managers
    }

    with pytest.raises(RuntimeError, match="Multiple server records"):
        await manager_smoke.main()

    assert FakeGateway.inventory_calls == [31, 32]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["unexpected-private-value", None, "", "missing"])
async def test_main_rejects_unknown_backend_before_any_gateway_read(
    monkeypatch, capsys, backend
):
    manager = SimpleNamespace(id=41, backend_type="openvpn-manager")
    unknown = SimpleNamespace(id=42)
    if backend != "missing":
        unknown.backend_type = backend
    install_fleet(monkeypatch, [manager, unknown])
    FakeGateway.statuses[41] = manager_status("manager-41")

    with pytest.raises(RuntimeError, match="unsupported backend") as caught:
        await manager_smoke.main()

    assert "server 42" in str(caught.value)
    assert "unexpected-private-value" not in str(caught.value)
    assert FakeGateway.inventory_calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_main_does_not_turn_node_only_fleet_into_manager_success(
    monkeypatch, capsys
):
    install_fleet(monkeypatch, [SimpleNamespace(id=51, backend_type="node-agent")])

    with pytest.raises(RuntimeError, match="OpenVPN Manager backend"):
        await manager_smoke.main()

    assert FakeGateway.inventory_calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_main_rejects_empty_fleet(monkeypatch, capsys):
    install_fleet(monkeypatch, [])

    with pytest.raises(RuntimeError, match="requires at least one server"):
        await manager_smoke.main()

    assert FakeGateway.inventory_calls == []
    assert capsys.readouterr().out == ""
