from datetime import datetime, timezone

from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler

from core.config import settings

from .billing_tasks import (
    charge_all_and_notify,
    publish_notification_outbox,
    reconcile_referral_rewards,
    reconcile_vpn_operations,
)

REFERRAL_RECONCILE_INTERVAL = 300


def main() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    scheduler = Scheduler(
        queue=Queue("billing", connection=redis_conn),
        connection=redis_conn,
        interval=settings.billing_interval,
    )

    if "charge_all_job" not in scheduler:
        now = datetime.now(timezone.utc)
        next_period = (
            int(now.timestamp()) // settings.billing_interval + 1
        ) * settings.billing_interval
        scheduler.schedule(
            # A Redis restore/recreate must not trigger an immediate extra
            # charge. Start on the next stable UTC billing boundary instead.
            scheduled_time=datetime.fromtimestamp(next_period, tz=timezone.utc),
            func=charge_all_and_notify,
            id="charge_all_job",
            interval=settings.billing_interval,
            repeat=None,
        )

    if "reconcile_vpn_operations_job" not in scheduler:
        reconcile_interval = min(300, max(60, settings.billing_interval))
        now = datetime.now(timezone.utc)
        next_reconcile = (
            int(now.timestamp()) // reconcile_interval + 1
        ) * reconcile_interval
        scheduler.schedule(
            scheduled_time=datetime.fromtimestamp(next_reconcile, tz=timezone.utc),
            func=reconcile_vpn_operations,
            id="reconcile_vpn_operations_job",
            interval=reconcile_interval,
            repeat=None,
        )

    if "reconcile_referral_rewards_job" not in scheduler:
        now = datetime.now(timezone.utc)
        next_referral_reconcile = (
            int(now.timestamp()) // REFERRAL_RECONCILE_INTERVAL + 1
        ) * REFERRAL_RECONCILE_INTERVAL
        scheduler.schedule(
            scheduled_time=datetime.fromtimestamp(
                next_referral_reconcile, tz=timezone.utc
            ),
            func=reconcile_referral_rewards,
            id="reconcile_referral_rewards_job",
            interval=REFERRAL_RECONCILE_INTERVAL,
            repeat=None,
        )

    if "publish_notification_outbox_job" not in scheduler:
        publish_interval = 30
        now = datetime.now(timezone.utc)
        next_publish = (int(now.timestamp()) // publish_interval + 1) * publish_interval
        scheduler.schedule(
            scheduled_time=datetime.fromtimestamp(next_publish, tz=timezone.utc),
            func=publish_notification_outbox,
            id="publish_notification_outbox_job",
            interval=publish_interval,
            repeat=None,
        )

    scheduler.run()


if __name__ == "__main__":
    main()
