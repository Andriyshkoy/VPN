from typing import Generic, Sequence, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class BaseRepo(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, obj: T) -> T:
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, **filters) -> T | None:
        stmt = select(self.model).filter_by(**filters)
        res = await self.session.scalar(stmt)
        return res

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        **filters,
    ) -> Sequence[T]:
        stmt = (
            select(self.model)
            .filter_by(**filters)
            .offset(offset)
            .limit(limit)
        )
        res = await self.session.scalars(stmt)
        return res.all()

    async def delete(self, **filters) -> int:
        stmt = delete(self.model).filter_by(**filters)
        res = await self.session.execute(stmt)
        return res.rowcount
