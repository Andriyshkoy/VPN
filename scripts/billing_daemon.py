import os
import sys

# Ensure project root is on path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService


async def run():
    billing = BillingService(uow, per_config_cost=settings.per_config_cost)
    while True:
        await billing.charge_all()
        await asyncio.sleep(settings.billing_interval)

if __name__ == "__main__":
    asyncio.run(run())
