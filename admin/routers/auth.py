from fastapi import APIRouter, HTTPException

from core.config import settings
from ..schemas import Login
from .. import auth as auth_utils

router = APIRouter()


@router.post("/login")
async def login(data: Login):
    if not (settings.admin_username and settings.admin_password_hash):
        raise HTTPException(status_code=503, detail="Login disabled")
    if data.username != settings.admin_username or not auth_utils.verify_password(data.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = auth_utils.generate_token()
    return {"token": token}
