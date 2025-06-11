from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from core.config import settings
from core.db.unit_of_work import uow
from core.exceptions import (
    ConfigNotFoundError,
    InsufficientBalanceError,
    ServerNotFoundError,
    UserNotFoundError,
)
from core.services import BillingService, ConfigService, ServerService, UserService

from .schemas import (
    ConfigListParams,
    ServerCreate,
    ServerListParams,
    ServerUpdate,
    TopUp,
    UserCreate,
    UserListParams,
    UserUpdate,
)
from .utils import serialize_dataclass
from . import auth

app = FastAPI()

server_service = ServerService(uow)
config_service = ConfigService(uow)
user_service = UserService(uow)
billing_service = BillingService(uow, per_config_cost=settings.per_config_cost)

origins = [
    "http://localhost",
    "http://localhost:5173",
    "https://andriyshkoy.ru",
    "https://vpn.andriyshkoy.ru",
    "https://admin.vpn.andriyshkoy.ru",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@app.exception_handler(InsufficientBalanceError)
async def insufficient_balance_handler(request: Request, exc: InsufficientBalanceError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Insufficient balance"},
    )


@app.exception_handler(ConfigNotFoundError)
async def config_not_found_handler(request: Request, exc: ConfigNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Config not found"},
    )


@app.exception_handler(ServerNotFoundError)
async def server_not_found_handler(request: Request, exc: ServerNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Server not found"},
    )


@app.exception_handler(UserNotFoundError)
async def user_not_found_handler(request: Request, exc: UserNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "User not found"},
    )


def require_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split()[1]
        if auth.token_valid(token):
            return

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


class Login(BaseModel):
    username: str
    password: str


@app.post("/login")
async def login(data: Login):
    if not (settings.admin_username and settings.admin_password_hash):
        raise HTTPException(status_code=503, detail="Login disabled")
    if data.username != settings.admin_username or not auth.verify_password(data.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = auth.generate_token()
    return {"token": token}


# ---------------------------------------------------------------------------
# Server endpoints
# ---------------------------------------------------------------------------


@app.get("/api/servers", dependencies=[Depends(auth_required)])
async def list_servers(params: ServerListParams = Depends()):
    servers = await server_service.list(
        limit=params.limit,
        offset=params.offset,
        host=params.host,
        location=params.location,
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
async def list_users(params: UserListParams = Depends()):
    users = await user_service.list(
        limit=params.limit,
        offset=params.offset,
        username=params.username,
        tg_id=params.tg_id,
    )
    return [serialize_dataclass(u) for u in users]


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
    user = await user_service.update(
        user_id, **data.model_dump(exclude_none=True)
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return serialize_dataclass(user)


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
async def list_configs(params: ConfigListParams = Depends()):
    configs = await config_service.list(
        limit=params.limit,
        offset=params.offset,
        server_id=params.server_id,
        owner_id=params.owner_id,
        suspended=params.suspended,
    )
    return [serialize_dataclass(c) for c in configs]


@app.get("/api/configs/{config_id}", dependencies=[Depends(auth_required)])
async def get_config(config_id: int):
    cfg = await config_service.get(config_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return serialize_dataclass(cfg)
