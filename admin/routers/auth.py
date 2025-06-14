from fastapi import APIRouter, HTTPException, status

from core.config import Settings
from ..schemas import Login
from .. import auth as auth_utils

router = APIRouter()


@router.post("/login")
async def login(data: Login):
    settings = Settings()
    if not (settings.admin_username and settings.admin_password_hash):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Login disabled"
        )
    if data.username != settings.admin_username or not auth_utils.verify_password(
        data.password
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid credentials"
        )
    token = auth_utils.generate_token()
    return {"token": token}
