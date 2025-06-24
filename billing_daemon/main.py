from multiprocessing import Process


from core.config import settings

from .rq_worker import run_worker
from .scheduler import bootstrap_schedule




if __name__ == "__main__":
    worker = Process(target=run_worker, daemon=True)
    worker.start()
    bootstrap_schedule()
