from redis import Redis
from rq import Connection, Queue, Worker

from core.config import settings


def run_worker() -> None:
    redis_conn = Redis.from_url(settings.redis_url)
    with Connection(redis_conn):
        worker = Worker([Queue("billing")])
        worker.work()


if __name__ == "__main__":
    run_worker()
