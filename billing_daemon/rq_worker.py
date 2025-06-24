from redis import Redis
from rq import Queue, Worker

from core.config import settings


def run_worker() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    queue = Queue("billing", connection=redis_conn, default_timeout=3600)
    worker = Worker([queue], connection=redis_conn)
    worker.work(logging_level="INFO")


if __name__ == "__main__":
    run_worker()
