from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from core.exceptions import (
    ConfigNotFoundError,
    InsufficientBalanceError,
    ServerNotFoundError,
    UserNotFoundError,
)


async def insufficient_balance_handler(request: Request, exc: InsufficientBalanceError):
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "Insufficient balance"},
    )


async def config_not_found_handler(request: Request, exc: ConfigNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Config not found"},
    )


async def server_not_found_handler(request: Request, exc: ServerNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "Server not found"},
    )


async def user_not_found_handler(request: Request, exc: UserNotFoundError):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"detail": "User not found"},
    )


def register_exception_handlers(app: FastAPI):
    app.add_exception_handler(InsufficientBalanceError, insufficient_balance_handler)
    app.add_exception_handler(ConfigNotFoundError, config_not_found_handler)
    app.add_exception_handler(ServerNotFoundError, server_not_found_handler)
    app.add_exception_handler(UserNotFoundError, user_not_found_handler)
