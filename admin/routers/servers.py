from fastapi import APIRouter, Depends, HTTPException, status

from core.db.unit_of_work import uow
from core.services import ServerService
from ..schemas import ServerCreate, ServerListParams, ServerUpdate
from ..utils import serialize_dataclass
from ..dependencies import auth_required

router = APIRouter(
    prefix="/api/servers",
    tags=["servers"],
    dependencies=[Depends(auth_required)],
)

server_service = ServerService(uow)


@router.get("")
async def list_servers(params: ServerListParams = Depends()):
    servers = await server_service.list(
        limit=params.limit,
        offset=params.offset,
        host=params.host,
        location=params.location,
    )
    return [serialize_dataclass(s) for s in servers]


@router.get("/{server_id}")
async def get_server(server_id: int):
    server = await server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return serialize_dataclass(server)


@router.post("")
async def create_server(data: ServerCreate):
    server = await server_service.create(
        name=data.name,
        ip=data.ip,
        port=data.port,
        host=data.host,
        location=data.location,
        api_key=data.api_key,
        cost=data.cost,
    )
    return serialize_dataclass(server)


@router.patch("/{server_id}")
async def update_server(server_id: int, data: ServerUpdate):
    server = await server_service.update(
        server_id, **data.model_dump(exclude_none=True)
    )
    if not server:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Server not found")
    return serialize_dataclass(server)


@router.delete("/{server_id}")
async def delete_server(server_id: int):
    deleted = await server_service.delete(server_id)
    return {"deleted": deleted}
