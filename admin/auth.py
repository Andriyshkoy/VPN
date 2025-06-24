import secrets
from typing import Optional

import redis.asyncio as redis

import bcrypt

from core.config import settings

TOKEN_TTL = 3600  # seconds
_redis: Optional[redis.Redis] = None

def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def generate_token() -> str:
    token = secrets.token_urlsafe(32)
    await _get_redis().setex(token, TOKEN_TTL, "1")
    return token


async def token_valid(token: str) -> bool:
    return await _get_redis().exists(token) == 1


def verify_password(password: str) -> bool:
    if not settings.admin_password_hash:
        return False
    return bcrypt.checkpw(password.encode(), settings.admin_password_hash.encode())
