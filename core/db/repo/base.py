# core/db/repo/base.py
from typing import Generic, Sequence, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

T = TypeVar("T")


class BaseRepo(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession):
        self.session = session

    async def add(self, obj: T) -> T:
        """
        Add a new object to the database.

        Args:
            obj: The object to add

        Returns:
            The added object with updated attributes from the database
        """
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def get(self, **filters) -> T | None:
        """
        Get a single object by filters.

        Args:
            **filters: Filter arguments to match

        Returns:
            Found object or None if not found
        """
        stmt = select(self.model).filter_by(**filters).options(
            selectinload(self.model.relations)
        )
        res = await self.session.scalar(stmt)
        return res

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
        **filters,
    ) -> Sequence[T]:
        """
        Get a list of objects matching the filters with pagination.

        Args:
            offset: Number of records to skip
            limit: Maximum number of records to return
            **filters: Filter arguments to match

        Returns:
            Sequence of objects matching the criteria
        """
        stmt = (
            select(self.model)
            .filter_by(**filters)
            .offset(offset)
            .limit(limit)
        )
        res = await self.session.scalars(stmt)
        return res.all()

    async def delete(self, **filters) -> int:
        """
        Delete objects matching the filters.

        Args:
            **filters: Filter arguments to match

        Returns:
            Number of deleted rows
        """
        stmt = delete(self.model).filter_by(**filters)
        res = await self.session.execute(stmt)
        return res.rowcount
