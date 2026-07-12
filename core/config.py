from decimal import Decimal

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
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
    # Manager transport remains HTTP by default for backwards compatibility.
    # When TLS is enabled, an explicit CA is optional (system trust is used),
    # while the client certificate/key pair enables mutual TLS.
    vpn_manager_tls_enabled: bool = False
    vpn_manager_mtls_required: bool = False
    vpn_manager_tls_port: int | None = Field(default=None, ge=1, le=65_535)
    vpn_manager_ca_cert_path: str = ""
    vpn_manager_client_cert_path: str = ""
    vpn_manager_client_key_path: str = ""
    vpn_drift_repair_enabled: bool = False
    telegram_update_poll_timeout: int = Field(default=30, ge=1, le=50)
    telegram_update_batch_size: int = Field(default=100, ge=1, le=100)
    telegram_update_processor_count: int = Field(default=4, ge=1, le=16)
    telegram_update_lease_seconds: int = Field(default=300, ge=30, le=3600)
    telegram_update_handler_timeout_seconds: int = Field(default=240, ge=5, le=3500)
    telegram_update_max_attempts: int = Field(default=20, ge=1, le=1000)
    telegram_update_retry_max_seconds: int = Field(default=300, ge=1, le=3600)
    telegram_update_retention_days: int = Field(default=30, ge=1, le=3650)
    telegram_update_dead_retention_days: int = Field(default=7, ge=1, le=3650)
    observability_enabled: bool = False
    statsd_host: str = "statsd_exporter"
    statsd_port: int = Field(default=9125, ge=1, le=65_535)
    vpn_hub_service: str = Field(default="unknown", min_length=1, max_length=48)
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    sql_echo: bool = False

    @model_validator(mode="after")
    def validate_manager_tls_settings(self) -> "Settings":
        """Validate TLS relationships without touching runtime secret mounts."""

        ca_path = self.vpn_manager_ca_cert_path.strip()
        cert_path = self.vpn_manager_client_cert_path.strip()
        key_path = self.vpn_manager_client_key_path.strip()
        if self.vpn_manager_mtls_required:
            if not self.vpn_manager_tls_enabled:
                raise ValueError("VPN Manager mTLS requires TLS to be enabled")
            if not ca_path or not cert_path or not key_path:
                raise ValueError(
                    "VPN Manager mTLS requires CA, client certificate, and key paths"
                )
        if self.vpn_manager_tls_enabled and bool(cert_path) != bool(key_path):
            raise ValueError(
                "VPN Manager client certificate and key must be configured together"
            )
        return self

    @model_validator(mode="after")
    def validate_telegram_update_settings(self) -> "Settings":
        """Keep handler cancellation and terminal retention fail-safe."""

        if (
            self.telegram_update_handler_timeout_seconds
            >= self.telegram_update_lease_seconds
        ):
            raise ValueError(
                "Telegram update handler timeout must be shorter than its lease"
            )
        if (
            self.telegram_update_dead_retention_days
            >= self.telegram_update_retention_days
        ):
            raise ValueError(
                "Telegram dead-update retention must be shorter than processed "
                "update retention"
            )
        return self

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
