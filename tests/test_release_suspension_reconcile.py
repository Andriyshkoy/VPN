from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import deploy.reconcile_suspended_configs as recovery


@pytest.mark.asyncio
async def test_release_reconcile_targets_exact_durable_operations(monkeypatch):
    target = recovery.SuspensionTarget(
        config_id=41,
        operation_id="00000000-0000-4000-9000-000000000041",
    )
    snapshots = iter(((target,), ()))
    executed: list[list[str]] = []
    verified: list[tuple[str, ...]] = []

    async def load_targets():
        return next(snapshots)

    class FakeConfigService:
        def __init__(self, unit_of_work):
            assert unit_of_work is recovery.uow

        async def execute_operations(self, operation_ids):
            executed.append(list(operation_ids))
            return len(operation_ids)

    async def assert_succeeded(operation_ids):
        verified.append(operation_ids)

    monkeypatch.setattr(recovery, "_load_targets", load_targets)
    monkeypatch.setattr(recovery, "_assert_succeeded", assert_succeeded)
    monkeypatch.setattr(recovery, "ConfigService", FakeConfigService)

    assert await recovery.reconcile_suspended_configs(timeout_seconds=1) == 1
    assert executed == [[target.operation_id]]
    assert verified == [(target.operation_id,)]


@pytest.mark.asyncio
async def test_release_reconcile_is_noop_without_suspension_drift(monkeypatch):
    async def load_targets():
        return ()

    class FakeConfigService:
        def __init__(self, unit_of_work):
            assert unit_of_work is recovery.uow

        async def execute_operations(self, operation_ids):
            raise AssertionError("No operations should be executed")

    monkeypatch.setattr(recovery, "_load_targets", load_targets)
    monkeypatch.setattr(recovery, "ConfigService", FakeConfigService)

    assert await recovery.reconcile_suspended_configs(timeout_seconds=1) == 0


@pytest.mark.asyncio
async def test_release_reconcile_rejects_incomplete_succeeded_operation(monkeypatch):
    operation_id = "00000000-0000-4000-9000-000000000044"
    operation = SimpleNamespace(
        operation_id=operation_id,
        status=recovery.VPNOperationStatus.SUCCEEDED.value,
        lease_token=None,
        lease_until=None,
        completed_at=None,
    )

    class FakeOperationsRepo:
        async def list_by_operation_ids(self, operation_ids):
            assert operation_ids == (operation_id,)
            return [operation]

    class FakeUow:
        async def __aenter__(self):
            return {"vpn_operations": FakeOperationsRepo()}

        async def __aexit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(recovery, "uow", lambda: FakeUow())

    with pytest.raises(RuntimeError, match="did not converge cleanly"):
        await recovery._assert_succeeded((operation_id,))


@pytest.mark.asyncio
async def test_release_reconcile_cancels_blocking_executor_at_deadline(monkeypatch):
    target = recovery.SuspensionTarget(
        config_id=42,
        operation_id="00000000-0000-4000-9000-000000000042",
    )
    cancelled = asyncio.Event()

    async def load_targets():
        return (target,)

    class FakeConfigService:
        def __init__(self, unit_of_work):
            assert unit_of_work is recovery.uow

        async def execute_operations(self, operation_ids):
            assert operation_ids == [target.operation_id]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    monkeypatch.setattr(recovery, "_load_targets", load_targets)
    monkeypatch.setattr(recovery, "ConfigService", FakeConfigService)

    with pytest.raises(RuntimeError, match="timed out with 1 pending"):
        await recovery.reconcile_suspended_configs(timeout_seconds=0.02)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_release_reconcile_rejects_unpinned_manager_before_execution(
    monkeypatch,
):
    operation_id = "00000000-0000-4000-9000-000000000045"
    config = (45, "expected-client", 7, 11, None, operation_id)

    class FakeRows:
        def all(self):
            return [config]

    class FakeSession:
        async def execute(self, statement):
            return FakeRows()

    class FakeUow:
        async def __aenter__(self):
            return {"configs": SimpleNamespace(session=FakeSession())}

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_uow():
        return FakeUow()

    class FakeConfigService:
        def __init__(self, unit_of_work):
            assert unit_of_work is fake_uow

        async def execute_operations(self, operation_ids):
            raise AssertionError("Unpinned Manager must fail before remote execution")

    monkeypatch.setattr(recovery, "uow", fake_uow)
    monkeypatch.setattr(recovery, "ConfigService", FakeConfigService)

    with pytest.raises(RuntimeError, match="without pinned identity"):
        await recovery.reconcile_suspended_configs(timeout_seconds=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatched_field", ["config_name", "server_id", "owner_id"])
async def test_release_reconcile_rejects_operation_snapshot_mismatch_before_execution(
    monkeypatch,
    mismatched_field,
):
    operation_id = "00000000-0000-4000-9000-000000000043"
    config = (
        43,
        "expected-client",
        7,
        11,
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        operation_id,
    )
    operation_values = {
        "operation_id": operation_id,
        "config_id": config[0],
        "config_name": config[1],
        "server_id": config[2],
        "owner_id": config[3],
        "kind": recovery.VPNOperationKind.SUSPEND.value,
        "status": recovery.VPNOperationStatus.PENDING.value,
    }
    operation_values[mismatched_field] = {
        "config_name": "different-client",
        "server_id": 8,
        "owner_id": 12,
    }[mismatched_field]
    operation = SimpleNamespace(**operation_values)

    class FakeRows:
        def all(self):
            return [config]

    class FakeScalars:
        def all(self):
            return [operation]

    class FakeSession:
        async def execute(self, statement):
            return FakeRows()

        async def scalars(self, statement):
            return FakeScalars()

    class FakeUow:
        async def __aenter__(self):
            return {"configs": SimpleNamespace(session=FakeSession())}

        async def __aexit__(self, exc_type, exc, tb):
            return None

    def fake_uow():
        return FakeUow()

    class FakeConfigService:
        def __init__(self, unit_of_work):
            assert unit_of_work is fake_uow

        async def execute_operations(self, operation_ids):
            raise AssertionError("Snapshot mismatch must fail before remote execution")

    monkeypatch.setattr(recovery, "uow", fake_uow)
    monkeypatch.setattr(recovery, "ConfigService", FakeConfigService)

    with pytest.raises(RuntimeError, match="snapshot does not match"):
        await recovery.reconcile_suspended_configs(timeout_seconds=1)
