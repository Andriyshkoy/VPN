from sqlalchemy import func, update

from core.db.models import BillingSettings

from .base import BaseRepo


class BillingSettingsRepo(BaseRepo[BillingSettings]):
    model = BillingSettings

    async def get_or_create(self) -> BillingSettings:
        settings = await self.get(id=1)
        if settings:
            return settings
        settings = self.model(id=1)
        return await self.add(settings)

    async def update(self, **kwargs) -> BillingSettings:
        stmt = (
            update(self.model)
            .where(self.model.id == 1)
            .values(**kwargs, updated_at=func.now())
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return result.scalar_one_or_none()
