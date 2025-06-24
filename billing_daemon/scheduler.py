import os
from datetime import datetime

import rq_scheduler
from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler

from billing_tasks import charge_all_and_notify
from core.config import settings

QUEUE_NAME = "billing"


def main() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    scheduler = Scheduler(queue=Queue(QUEUE_NAME, connection=redis_conn),
                          connection=redis_conn)

    if scheduler.get_job("charge_all_job") is None:
        scheduler.schedule(
            scheduled_time=datetime.utcnow(),
            func=charge_all_and_notify,
            interval=settings.billing_interval,
            id="charge_all_job",
        )

    # Бесконечная работа планировщика
    scheduler.run()


if __name__ == "__main__":
    main()
