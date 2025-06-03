# core/db/types/encrypted.py

from cryptography.fernet import Fernet
from sqlalchemy.types import LargeBinary, TypeDecorator

from core.config import settings


class EncryptedString(TypeDecorator):
    """Transparent AES-128-GCM (Fernet) field."""
    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        fernet = Fernet(settings.encryption_key)
        return fernet.encrypt(value.encode())

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        fernet = Fernet(settings.encryption_key)
        return fernet.decrypt(value).decode()
