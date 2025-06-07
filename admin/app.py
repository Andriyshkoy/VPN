from dataclasses import asdict

from sanic import Sanic
from sanic.request import Request
from sanic.response import json, file
from sanic.exceptions import Unauthorized, NotFound, InvalidUsage
from pydantic import ValidationError

from .schemas import ServerCreate, ServerUpdate, ConfigCreate, TopUp
from core.config import settings
from core.db.unit_of_work import uow
from core.services import (
    BillingService,
    Config,
    ConfigService,
    ServerService,
    User,
    UserService,
)

app = Sanic("admin_api")

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)


def require_auth(request: Request) -> None:
    api_key = settings.admin_api_key
    if not api_key:
        return
    key = request.headers.get("X-API-Key")
    if key != api_key:
        raise Unauthorized()


def auth_required(handler):
    async def wrapper(request: Request, *args, **kwargs):
        require_auth(request)
        return await handler(request, *args, **kwargs)

    return wrapper


def parse(model, request: Request):
    data = request.json or {}
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise InvalidUsage(exc.errors())


@app.get("/")
@auth_required
async def index(request: Request):
    return json({"status": "ok"})


@app.get("/servers")
@auth_required
async def list_servers(request: Request):
    servers = await server_service.list()
    return json([asdict(s) for s in servers])


@app.post("/servers")
@auth_required
async def create_server(request: Request):
    data = parse(ServerCreate, request)
    server = await server_service.create(
        name=data.name,
        ip=data.ip,
        port=data.port,
        host=data.host,
        location=data.location,
        api_key=data.api_key,
        cost=data.cost,
    )
    return json(asdict(server))


@app.put("/servers/<server_id:int>")
@auth_required
async def update_server(request: Request, server_id: int):
    data = parse(ServerUpdate, request)
    srv = await server_service.update(server_id, **data.model_dump(exclude_none=True))
    if not srv:
        raise NotFound()
    return json(asdict(srv))


@app.delete("/servers/<server_id:int>")
@auth_required
async def delete_server(request: Request, server_id: int):
    deleted = await server_service.delete(server_id)
    return json({"deleted": deleted})


@app.get("/configs")
@auth_required
async def list_configs(request: Request):
    configs = await config_service.list_active()
    return json([asdict(c) for c in configs])


@app.post("/configs")
@auth_required
async def create_config(request: Request):
    data = parse(ConfigCreate, request)
    cfg = await config_service.create_config(
        server_id=data.server_id,
        owner_id=data.owner_id,
        name=data.name,
        display_name=data.display_name or data.name,
        use_password=data.use_password,
    )
    return json(asdict(cfg))


@app.get("/configs/<config_id:int>/download")
@auth_required
async def download_config(request: Request, config_id: int):
    content = await config_service.download_config(config_id)
    path = f"/tmp/config_{config_id}.ovpn"
    with open(path, "wb") as f:
        f.write(content)
    return await file(path, filename=f"config_{config_id}.ovpn")


@app.delete("/configs/<config_id:int>")
@auth_required
async def delete_config(request: Request, config_id: int):
    await config_service.revoke_config(config_id)
    return json({"deleted": True})


@app.get("/users")
@auth_required
async def list_users(request: Request):
    users = await user_service.list()
    return json([asdict(u) for u in users])


@app.get("/users/<user_id:int>")
@auth_required
async def view_user(request: Request, user_id: int):
    user, configs = await user_service.get_with_configs(user_id)
    if not user:
        raise NotFound()
    return json({"user": asdict(user), "configs": [asdict(c) for c in configs]})


@app.post("/users/<user_id:int>/topup")
@auth_required
async def top_up(request: Request, user_id: int):
    data = parse(TopUp, request)
    user = await billing_service.top_up(user_id, data.amount)
    return json(asdict(user))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
