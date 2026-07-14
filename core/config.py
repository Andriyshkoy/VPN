import ipaddress
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
    admin_session_ttl_seconds: int = Field(default=28_800, ge=900, le=604_800)
    # Production stays fail-closed on HTTPS-only cookies. Local Docker may
    # explicitly disable this flag because it is only exposed on loopback.
    admin_cookie_secure: bool = True
    # Transitional bearer-token endpoints are disabled unless an operator
    # explicitly enables them for a short rollback window.
    admin_legacy_api_enabled: bool = False
    admin_trusted_proxy_cidrs: str = "127.0.0.0/8,::1/128"
    admin_login_max_failures: int = Field(default=5, ge=3, le=20)
    admin_login_lockout_seconds: int = Field(default=900, ge=60, le=86_400)
    admin_login_rate_limit_attempts: int = Field(default=20, ge=5, le=100)
    admin_login_rate_limit_window_seconds: int = Field(default=300, ge=60, le=3600)
    admin_action_stale_seconds: int = Field(default=300, ge=120, le=3600)
    admin_fleet_status_stale_seconds: int = Field(default=300, ge=30, le=86_400)
    admin_fleet_status_retention_per_server: int = Field(
        default=1_000, ge=10, le=100_000
    )
    admin_fleet_poll_enabled: bool = True
    admin_fleet_poll_interval_seconds: int = Field(default=60, ge=30, le=3_600)
    admin_fleet_poll_concurrency: int = Field(default=4, ge=1, le=32)
    telegram_pay_token: str = ""
    redis_url: str = "redis://localhost:6379/0"

    # Operational kill switches.  They deliberately live below the UI layer
    # so a broken bot/admin frontend cannot re-enable critical background work.
    maintenance_mode: bool = False
    billing_enabled: bool = True
    payments_enabled: bool = True
    provisioning_enabled: bool = True
    notifications_enabled: bool = True
    notification_max_attempts: int = Field(default=10, ge=1, le=100)
    notification_visibility_timeout: int = Field(default=120, ge=30, le=3600)
    notification_dedupe_ttl_seconds: int = Field(default=86_400, ge=3600, le=604_800)
    payment_intent_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    # Referral rewards are issued as non-withdrawable service balance only for
    # newly credited provider payments. Rates use basis points so policy values
    # are exact (500 bps = 5%) and can be snapshotted in the audit trail.
    referral_rewards_enabled: bool = True
    referral_level_1_rate_bps: int = Field(default=500, ge=0, le=1_000)
    referral_level_2_rate_bps: int = Field(default=100, ge=0, le=1_000)
    referral_program_version: str = Field(
        default="v1-5pct-1pct", min_length=1, max_length=32
    )
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
    prometheus_api_url: str = ""
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
    def validate_admin_fleet_poll_settings(self) -> "Settings":
        if (
            self.admin_fleet_poll_enabled
            and self.admin_fleet_status_stale_seconds
            < 2 * self.admin_fleet_poll_interval_seconds
        ):
            raise ValueError(
                "Admin fleet status stale window must cover at least two poll intervals"
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

    @model_validator(mode="after")
    def validate_referral_reward_settings(self) -> "Settings":
        """Keep the two-level referral policy bounded and unambiguous."""

        if self.referral_level_2_rate_bps > self.referral_level_1_rate_bps:
            raise ValueError("Level 2 referral rate cannot exceed level 1")
        if self.referral_level_1_rate_bps + self.referral_level_2_rate_bps > 1_000:
            raise ValueError("Combined referral rates cannot exceed 10%")
        if not self.referral_program_version.strip():
            raise ValueError("Referral program version cannot be blank")
        policy = (
            self.referral_program_version,
            self.referral_level_1_rate_bps,
            self.referral_level_2_rate_bps,
        )
        if policy != ("v1-5pct-1pct", 500, 100):
            # Unsettled captures are deliberately processed by the same fixed
            # contract after a pause. A future rate change needs a versioned
            # code/migration release rather than an in-place environment edit.
            raise ValueError("Unsupported referral program policy")
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

    @field_validator("admin_trusted_proxy_cidrs")
    @classmethod
    def validate_admin_trusted_proxy_cidrs(cls, value: str) -> str:
        entries = [item.strip() for item in value.split(",") if item.strip()]
        if not entries:
            raise ValueError("ADMIN_TRUSTED_PROXY_CIDRS cannot be empty")
        try:
            for entry in entries:
                ipaddress.ip_network(entry, strict=False)
        except ValueError as exc:
            raise ValueError(
                "ADMIN_TRUSTED_PROXY_CIDRS contains an invalid CIDR"
            ) from exc
        return ",".join(entries)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
