from pydantic import BaseModel, Field


class ServerCreate(BaseModel):
    name: str
    ip: str
    port: int = 22
    host: str
    location: str
    api_key: str
    cost: float = Field(default=0)


class ServerUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    port: int | None = None
    host: str | None = None
    location: str | None = None
    api_key: str | None = None
    cost: float | None = None


class ConfigCreate(BaseModel):
    server_id: int
    owner_id: int
    name: str
    display_name: str | None = None
    use_password: bool = False


class TopUp(BaseModel):
    amount: float


class UserCreate(BaseModel):
    tg_id: int
    username: str | None = None
    balance: float = 0.0


class UserUpdate(BaseModel):
    tg_id: int | None = None
    username: str | None = None
    balance: float | None = None
