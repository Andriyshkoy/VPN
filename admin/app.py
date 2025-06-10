from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ValidationError

from core.config import settings
from core.db.unit_of_work import uow
from core.services import BillingService, ConfigService, ServerService, UserService
from core.services.models import Config, User

from .schemas import (
    ConfigCreate,
    ServerCreate,
    ServerUpdate,
    TopUp,
    UserCreate,
    UserUpdate,
)
from .utils import serialize_dataclass

app = FastAPI()

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> None:
    api_key = settings.admin_api_key
    if not api_key:
        return
    key = request.headers.get("X-API-Key")
    if key != api_key:
        raise HTTPException(status_code=401, detail="Unauthorized")


def auth_required(request: Request):
    require_auth(request)


def parse(model: type[BaseModel], request: Request):
    try:
        data = (
            request.json() if callable(getattr(request, "json", None)) else request.json
        )
        if callable(data):
            data = request.json()
    except Exception:
        data = {}
    try:
        return model.model_validate(data or {})
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors())


# ---------------------------------------------------------------------------
# Server endpoints
# ---------------------------------------------------------------------------


@app.get("/api/servers", dependencies=[Depends(auth_required)])
async def list_servers(
    limit: int | None = None,
    offset: int = 0,
    host: str | None = None,
    location: str | None = None,
):
    servers = await server_service.list(
        limit=limit, offset=offset, host=host, location=location
    )
    return [serialize_dataclass(s) for s in servers]


@app.get("/api/servers/{server_id}", dependencies=[Depends(auth_required)])
async def get_server(server_id: int):
    server = await server_service.get(server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return serialize_dataclass(server)


@app.post("/api/servers", dependencies=[Depends(auth_required)])
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


@app.patch("/api/servers/{server_id}", dependencies=[Depends(auth_required)])
async def update_server(server_id: int, data: ServerUpdate):
    server = await server_service.update(
        server_id, **data.model_dump(exclude_none=True)
    )
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return serialize_dataclass(server)


@app.delete("/api/servers/{server_id}", dependencies=[Depends(auth_required)])
async def delete_server(server_id: int):
    deleted = await server_service.delete(server_id)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------


@app.get("/api/users", dependencies=[Depends(auth_required)])
async def list_users(
    limit: int | None = None,
    offset: int = 0,
    username: str | None = None,
    tg_id: int | None = None,
):
    async with uow() as repos:
        users = await repos["users"].list(
            limit=limit, offset=offset, username=username, tg_id=tg_id
        )
    return [serialize_dataclass(User.from_orm(u)) for u in users]


@app.post("/api/users", dependencies=[Depends(auth_required)])
async def create_user(data: UserCreate):
    user = await user_service.register(
        tg_id=data.tg_id, username=data.username, balance=data.balance
    )
    return serialize_dataclass(user)


@app.get("/api/users/{user_id}", dependencies=[Depends(auth_required)])
async def get_user(user_id: int):
    user = await user_service.get(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return serialize_dataclass(user)


@app.patch("/api/users/{user_id}", dependencies=[Depends(auth_required)])
async def update_user(user_id: int, data: UserUpdate):
    async with uow() as repos:
        user = await repos["users"].update(
            user_id, **data.model_dump(exclude_none=True)
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return serialize_dataclass(User.from_orm(user))


@app.delete("/api/users/{user_id}", dependencies=[Depends(auth_required)])
async def delete_user(user_id: int):
    deleted = await user_service.delete(user_id)
    return {"deleted": deleted}


@app.post("/api/users/{user_id}/topup", dependencies=[Depends(auth_required)])
async def topup_user(user_id: int, data: TopUp):
    user = await billing_service.top_up(user_id, data.amount)
    return serialize_dataclass(user)


@app.post("/api/users/{user_id}/withdraw", dependencies=[Depends(auth_required)])
async def withdraw_user(user_id: int, data: TopUp):
    user = await billing_service.withdraw(user_id, data.amount)
    return serialize_dataclass(user)


# ---------------------------------------------------------------------------
# Config endpoints
# ---------------------------------------------------------------------------


@app.get("/api/configs", dependencies=[Depends(auth_required)])
async def list_configs(
    limit: int | None = None,
    offset: int = 0,
    server_id: int | None = None,
    owner_id: int | None = None,
    suspended: bool | None = None,
):
    async with uow() as repos:
        configs = await repos["configs"].list(
            limit=limit,
            offset=offset,
            server_id=server_id,
            owner_id=owner_id,
            suspended=suspended,
        )
    return [serialize_dataclass(Config.from_orm(c)) for c in configs]


@app.get("/api/configs/{config_id}", dependencies=[Depends(auth_required)])
async def get_config(config_id: int):
    cfg = await config_service.get(config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return serialize_dataclass(cfg)
