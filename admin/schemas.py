from typing import Annotated, Optional

from pydantic import (
    BaseModel,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
)

Port = Annotated[int, Field(ge=1, le=65_535)]
Money = Annotated[float, Field(ge=0)]


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class ServerBase(BaseModel):
    name: Optional[str] = None
    ip: Optional[str] = None
    port: Optional[Port] = None
    host: Optional[str] = None
    location: Optional[str] = None
    api_key: Optional[str] = None
    monthly_cost: Optional[Money] = None


class ServerCreate(ServerBase):
    name: str
    ip: str
    port: Optional[Port] = Field(22, description="SSH-порт")
    host: str
    location: str
    api_key: str
    monthly_cost: Money = 0.0


class ServerUpdate(ServerBase):
    pass


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

class ConfigCreate(BaseModel):
    server_id: PositiveInt
    owner_id: PositiveInt
    name: str
    display_name: Optional[str] = None
    use_password: bool = False


# --------------------------------------------------------------------------- #
# User
# --------------------------------------------------------------------------- #

class TopUp(BaseModel):
    amount: PositiveFloat  # пополнять можно только > 0


class UserBase(BaseModel):
    tg_id: Optional[PositiveInt] = None
    username: Optional[str] = None
    balance: Optional[Money] = None


class UserCreate(UserBase):
    tg_id: PositiveInt
    balance: Money = 0.0  # стартовый баланс может быть 0


class UserUpdate(UserBase):
    pass


# --------------------------------------------------------------------------- #
# List parameters
# --------------------------------------------------------------------------- #

class Pagination(BaseModel):
    """limit/offset вынесены сюда, чтобы не повторяться в *ListParams."""

    limit: Optional[PositiveInt] = Field(
        None, description="Максимум объектов за запрос; None → без ограничения"
    )
    offset: NonNegativeInt = Field(
        0, description="Сколько объектов пропустить от начала выборки"
    )


class ServerListParams(Pagination):
    host: Optional[str] = None
    location: Optional[str] = None


class UserListParams(Pagination):
    username: Optional[str] = None
    tg_id: Optional[PositiveInt] = None


class ConfigListParams(Pagination):
    server_id: Optional[PositiveInt] = None
    owner_id: Optional[PositiveInt] = None
    suspended: Optional[bool] = None


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #

class Login(BaseModel):
    username: str
    password: str
