from datetime import datetime

from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler

from .billing_tasks import charge_all_and_notify
from core.config import settings


def main() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    scheduler = Scheduler(queue=Queue("billing", connection=redis_conn),
                          connection=redis_conn, interval=settings.billing_interval)

    if "charge_all_job" not in scheduler:
        scheduler.schedule(
            scheduled_time=datetime.now(),
            func=charge_all_and_notify,
            id="charge_all_job",
            interval=settings.billing_interval,
            repeat=None,
        )

    scheduler.run()


if __name__ == "__main__":
    main()
