from fastapi import HTTPException, Request, status
from pydantic import BaseModel, ValidationError

from . import auth


async def require_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        token = auth_header.split()[1]
        if await auth.token_valid(token):
            return
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def auth_required(request: Request):
    await require_auth(request)


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.errors())
