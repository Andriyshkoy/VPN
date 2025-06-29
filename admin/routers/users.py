from fastapi import APIRouter, Depends, HTTPException, status

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, UserService

from ..dependencies import auth_required
from ..schemas import TopUp, UserCreate, UserListParams, UserUpdate
from ..utils import serialize_dataclass

router = APIRouter(
    prefix="/api/users",
    tags=["users"],
    dependencies=[Depends(auth_required)],
)

user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)


@router.get("")
async def list_users(params: UserListParams = Depends()):
    users = await user_service.list(
        limit=params.limit,
        offset=params.offset,
        username=params.username,
        tg_id=params.tg_id,
    )
    return [serialize_dataclass(u) for u in users]


@router.post("")
async def create_user(data: UserCreate):
    user = await user_service.register(
        tg_id=data.tg_id, username=data.username, balance=data.balance
    )
    return serialize_dataclass(user)


@router.get("/{user_id}")
async def get_user(user_id: int):
    user = await user_service.get(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return serialize_dataclass(user)


@router.patch("/{user_id}")
async def update_user(user_id: int, data: UserUpdate):
    user = await user_service.update(
        user_id, **data.model_dump(exclude_none=True)
    )
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return serialize_dataclass(user)


@router.delete("/{user_id}")
async def delete_user(user_id: int):
    deleted = await user_service.delete(user_id)
    return {"deleted": deleted}


@router.post("/{user_id}/topup")
async def topup_user(user_id: int, data: TopUp):
    user = await billing_service.top_up(user_id, data.amount)
    return serialize_dataclass(user)


@router.post("/{user_id}/withdraw")
async def withdraw_user(user_id: int, data: TopUp):
    user = await billing_service.withdraw(user_id, data.amount)
    return serialize_dataclass(user)
