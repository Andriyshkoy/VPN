import secrets
import time
from typing import Dict

import bcrypt

from core.config import settings

TOKEN_TTL = 3600  # seconds
_tokens: Dict[str, float] = {}


def _prune() -> None:
    now = time.time()
    expired = [t for t, exp in _tokens.items() if exp < now]
    for t in expired:
        del _tokens[t]


def generate_token() -> str:
    _prune()
    token = secrets.token_urlsafe(32)
    _tokens[token] = time.time() + TOKEN_TTL
    return token


def token_valid(token: str) -> bool:
    _prune()
    exp = _tokens.get(token)
    if not exp or exp < time.time():
        _tokens.pop(token, None)
        return False
    return True


def verify_password(password: str) -> bool:
    if not settings.admin_password_hash:
        return False
    return bcrypt.checkpw(password.encode(), settings.admin_password_hash.encode())
