from decimal import Decimal

from cryptography.fernet import Fernet
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    encryption_key: str
    bot_token: str = ""
    per_config_cost: Decimal = Field(default=Decimal("1.00"), ge=0)
    config_creation_cost: Decimal = Field(default=Decimal("10.00"), ge=0)
    billing_interval: int = Field(default=3600, ge=60)
    admin_username: str = ""
    admin_password_hash: str = ""
    telegram_pay_token: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # Operational kill switches.  They deliberately live below the UI layer
    # so a broken bot/admin frontend cannot re-enable critical background work.
    maintenance_mode: bool = False
    billing_enabled: bool = True
    provisioning_enabled: bool = True
    notifications_enabled: bool = True
    notification_max_attempts: int = Field(default=10, ge=1, le=100)
    notification_visibility_timeout: int = Field(default=120, ge=30, le=3600)
    notification_dedupe_ttl_seconds: int = Field(default=86_400, ge=3600, le=604_800)
    payment_intent_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    vpn_operation_max_attempts: int = Field(default=20, ge=1, le=1000)
    sql_echo: bool = False

    @field_validator("encryption_key")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        """Fail at startup before an invalid key corrupts operational flows."""

        if not value or value == "change_me":
            raise ValueError("ENCRYPTION_KEY must be a stable Fernet key")
        try:
            Fernet(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("ENCRYPTION_KEY must be a valid Fernet key") from exc
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
