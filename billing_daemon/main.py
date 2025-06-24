import time
from threading import Thread


from redis import Redis
from rq import Queue

from core.config import settings

from .billing_tasks import charge_all_and_notify
from .rq_worker import run_worker




def _scheduler() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("billing", connection=redis_conn)
    while True:
        queue.enqueue(charge_all_and_notify)
        time.sleep(settings.billing_interval)


if __name__ == "__main__":
    Thread(target=run_worker, daemon=True).start()
    _scheduler()
