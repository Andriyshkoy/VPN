from datetime import datetime

from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler

from core.config import settings
from .billing_tasks import charge_all_and_notify


def bootstrap_schedule() -> None:
    """Initialize RQ scheduler for periodic billing job."""
    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("billing", connection=redis_conn, default_timeout=3600)
    scheduler = Scheduler(queue=queue, connection=redis_conn)

    if scheduler.get_job("charge_all_job") is None:
        scheduler.schedule(
            scheduled_time=datetime.utcnow(),
            func=charge_all_and_notify,
            interval=settings.billing_interval,
            id="charge_all_job",
        )
