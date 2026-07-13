from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from core.db.models.referral_reward import ReferralReward
from core.db.models.user import User
from core.exceptions import UserNotFoundError


@dataclass(frozen=True, slots=True)
class ReferralOverview:
    referral_code: str
    level_1_count: int
    level_2_count: int
    level_1_earned: Decimal
    level_2_earned: Decimal

    @property
    def total_earned(self) -> Decimal:
        return self.level_1_earned + self.level_2_earned


class ReferralService:
    """Read-only referral statistics for the account owner."""

    def __init__(self, uow: Callable):
        self._uow = uow

    async def overview(self, user_id: int) -> ReferralOverview:
        async with self._uow() as repos:
            session = repos["users"].session
            user = await session.get(User, user_id)
            if user is None:
                raise UserNotFoundError(f"User with ID {user_id} not found")

            direct = aliased(User)
            second = aliased(User)
            level_1_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(direct)
                    .where(direct.referred_by_id == user_id)
                )
                or 0
            )
            level_2_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(second)
                    .join(direct, second.referred_by_id == direct.id)
                    .where(direct.referred_by_id == user_id)
                )
                or 0
            )
            earned = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ReferralReward.level == 1,
                                        ReferralReward.reward_amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (
                                        ReferralReward.level == 2,
                                        ReferralReward.reward_amount,
                                    ),
                                    else_=Decimal("0.00"),
                                )
                            ),
                            Decimal("0.00"),
                        ),
                    ).where(ReferralReward.beneficiary_user_id == user_id)
                )
            ).one()

        return ReferralOverview(
            referral_code=user.referral_code,
            level_1_count=level_1_count,
            level_2_count=level_2_count,
            level_1_earned=Decimal(earned[0]),
            level_2_earned=Decimal(earned[1]),
        )
