import importlib
import types

import pytest

from core.db.unit_of_work import uow
from core.services import BillingService, ServerService, UserService


@pytest.mark.asyncio
async def test_charge_and_notify(monkeypatch, sessionmaker):
    class DummyGateway:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

        async def create_client(self, name, use_password=False):
            pass

        async def download_config(self, name):
            return b"data"

        async def revoke_client(self, name):
            pass

        async def suspend_client(self, name):
            pass

        async def unsuspend_client(self, name):
            pass

        async def list_blocked(self):
            return []

    monkeypatch.setattr(
        "core.services.config.APIGateway", lambda *a, **kw: DummyGateway()
    )
    monkeypatch.setenv("BOT_TOKEN", "token:1")
    import core.config as core_config

    core_config = importlib.reload(core_config)

    import billing_daemon.billing_tasks as billing_tasks

    billing_tasks = importlib.reload(billing_tasks)

    sent = []

    class DummyService:
        def __init__(self, *a, **kw):
            pass

        async def enqueue(self, chat_id, text, *, notification_id=None):
            sent.append((chat_id, text))

    monkeypatch.setattr(billing_tasks, "NotificationService", DummyService)

    user_svc = UserService(uow)
    server_svc = ServerService(uow)
    billing = BillingService(uow, per_config_cost=1)

    user = await user_svc.register(123)
    server = await server_svc.create(
        name="s",
        ip="1",
        port=22,
        host="h",
        location="loc",
        api_key="k",
        cost=1,
    )

    await billing.top_up(user.id, 30)
    await billing.create_paid_config(
        server_id=server.id,
        owner_id=user.id,
        name="cfg",
        display_name="d",
        creation_cost=5,
    )

    await billing_tasks._charge_all_and_notify_async()

    assert sent and "сутки" in sent[0][1]


@pytest.mark.asyncio
async def test_reconcile_referral_rewards(monkeypatch):
    import billing_daemon.billing_tasks as billing_tasks

    calls = []

    class DummyBillingService:
        def __init__(self, unit_of_work, *, per_config_cost):
            calls.append((unit_of_work, per_config_cost))

        async def reconcile_referral_rewards(self, *, limit):
            calls.append(limit)

    monkeypatch.setattr(billing_tasks, "BillingService", DummyBillingService)
    monkeypatch.setattr(billing_tasks.settings, "maintenance_mode", False)
    monkeypatch.setattr(billing_tasks.settings, "referral_rewards_enabled", True)

    assert await billing_tasks._reconcile_referral_rewards_async(limit=37) is True
    assert calls == [
        (billing_tasks.uow, billing_tasks.settings.per_config_cost),
        37,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("maintenance_mode", "referral_rewards_enabled"),
    [(True, True), (False, False), (True, False)],
)
async def test_reconcile_referral_rewards_respects_kill_switches(
    monkeypatch, maintenance_mode, referral_rewards_enabled
):
    import billing_daemon.billing_tasks as billing_tasks

    class UnexpectedBillingService:
        def __init__(self, *args, **kwargs):
            raise AssertionError("disabled referral reconciliation must not start")

    monkeypatch.setattr(billing_tasks, "BillingService", UnexpectedBillingService)
    monkeypatch.setattr(billing_tasks.settings, "maintenance_mode", maintenance_mode)
    monkeypatch.setattr(
        billing_tasks.settings,
        "referral_rewards_enabled",
        referral_rewards_enabled,
    )

    assert await billing_tasks._reconcile_referral_rewards_async() is False


def test_reconcile_referral_rewards_is_observed(monkeypatch):
    import billing_daemon.billing_tasks as billing_tasks

    calls = []

    async def dummy_reconcile():
        return False

    def dummy_observe(name, outcome, duration):
        calls.append((name, outcome, duration))

    monkeypatch.setattr(
        billing_tasks, "_reconcile_referral_rewards_async", dummy_reconcile
    )
    monkeypatch.setattr(billing_tasks, "observe_background_job", dummy_observe)

    billing_tasks.reconcile_referral_rewards()

    assert len(calls) == 1
    name, outcome, duration = calls[0]
    assert name == "referral_reconcile"
    assert outcome == "skipped"
    assert duration >= 0


def test_scheduler_registers_referral_reconciliation(monkeypatch):
    import billing_daemon.scheduler as scheduler_module

    scheduled = []
    scheduler_options = []

    class DummyScheduler:
        def __init__(self, **kwargs):
            scheduler_options.append(kwargs)
            self.jobs = {}

        def __contains__(self, job_id):
            return job_id in self.jobs

        def schedule(self, **kwargs):
            self.jobs[kwargs["id"]] = kwargs
            scheduled.append(kwargs)

        def run(self):
            return None

    monkeypatch.setattr(scheduler_module.Redis, "from_url", lambda url: object())
    monkeypatch.setattr(scheduler_module, "Queue", lambda *args, **kwargs: object())
    monkeypatch.setattr(scheduler_module, "Scheduler", DummyScheduler)
    monkeypatch.setattr(scheduler_module.settings, "billing_interval", 7200)

    scheduler_module.main()

    assert scheduler_options[0]["interval"] == 30

    charge_job = next(job for job in scheduled if job["id"] == "charge_all_job")
    assert charge_job["func"] is scheduler_module.charge_all_and_notify
    assert charge_job["interval"] == 7200

    referral_job = next(
        job for job in scheduled if job["id"] == "reconcile_referral_rewards_job"
    )
    assert referral_job["func"] is scheduler_module.reconcile_referral_rewards
    assert referral_job["interval"] == 300
    assert referral_job["repeat"] is None
