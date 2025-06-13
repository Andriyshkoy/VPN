from fastapi import APIRouter, Depends, HTTPException

from core.db.unit_of_work import uow
from core.services import ConfigService
from ..schemas import ConfigListParams
from ..utils import serialize_dataclass
from ..dependencies import auth_required

router = APIRouter(
    prefix="/api/configs",
    tags=["configs"],
    dependencies=[Depends(auth_required)],
)

config_service = ConfigService(uow)


@router.get("")
async def list_configs(params: ConfigListParams = Depends()):
    configs = await config_service.list(
        limit=params.limit,
        offset=params.offset,
        server_id=params.server_id,
        owner_id=params.owner_id,
        suspended=params.suspended,
    )
    return [serialize_dataclass(c) for c in configs]


@router.get("/{config_id}")
async def get_config(config_id: int):
    cfg = await config_service.get(config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return serialize_dataclass(cfg)
