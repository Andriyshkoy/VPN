from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise ValueError(f"{field} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} is invalid")
    return parsed


class AdminServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    ip: str = Field(min_length=1, max_length=64)
    port: int = Field(default=16290, ge=1, le=65535)
    host: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1, max_length=4096)
    monthly_cost: Decimal = Decimal("0.00")
    # Newly registered control-plane endpoints are quarantined until an
    # operator verifies Manager identity/health and explicitly activates them.
    lifecycle_state: Literal["disabled"] = "disabled"
    accepts_new_configs: Literal[False] = False
    max_configs: int | None = Field(default=None, ge=1, le=2_147_483_647)
    capacity_reserve: int = Field(default=0, ge=0, le=2_147_483_647)
    placement_weight: Decimal = Decimal("1")
    provider: str | None = Field(default=None, max_length=128)
    public_endpoint: str | None = Field(default=None, max_length=255)

    @field_validator("monthly_cost", mode="before")
    @classmethod
    def valid_cost(cls, value: object) -> Decimal:
        parsed = _decimal(value, "monthly_cost")
        if (
            parsed < 0
            or parsed > Decimal("99999999.99")
            or parsed.as_tuple().exponent < -2
        ):
            raise ValueError(
                "monthly_cost must be non-negative with at most 2 decimals"
            )
        return parsed

    @field_validator("placement_weight", mode="before")
    @classmethod
    def valid_weight(cls, value: object) -> Decimal:
        parsed = _decimal(value, "placement_weight")
        if parsed <= 0 or parsed > 1_000 or parsed.as_tuple().exponent < -3:
            raise ValueError(
                "placement_weight must be positive with at most 3 decimals"
            )
        return parsed

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value: str) -> str:
        if "\r" in value or "\n" in value or not value.strip():
            raise ValueError("api_key is invalid")
        return value.strip()

    @field_validator("name", "ip", "host", "location")
    @classmethod
    def required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("ip")
    @classmethod
    def valid_manager_host(cls, value: str) -> str:
        if any(character.isspace() for character in value) or any(
            marker in value for marker in ("://", "/", "?", "#", "@")
        ):
            raise ValueError("ip must be a Manager hostname or address without a port")
        return value

    @field_validator("provider", "public_endpoint")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def valid_capacity(self):
        if self.max_configs is not None and self.capacity_reserve >= self.max_configs:
            raise ValueError("capacity_reserve must be below max_configs")
        if self.lifecycle_state != "active" and self.accepts_new_configs:
            raise ValueError("only active servers may accept new configs")
        return self


class AdminServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    ip: str | None = Field(default=None, min_length=1, max_length=64)
    port: int | None = Field(default=None, ge=1, le=65535)
    host: str | None = Field(default=None, min_length=1, max_length=128)
    location: str | None = Field(default=None, min_length=1, max_length=128)
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    monthly_cost: Decimal | None = None
    max_configs: int | None = Field(default=None, ge=1, le=2_147_483_647)
    clear_max_configs: bool = False
    capacity_reserve: int | None = Field(default=None, ge=0, le=2_147_483_647)
    placement_weight: Decimal | None = None
    provider: str | None = Field(default=None, max_length=128)
    public_endpoint: str | None = Field(default=None, max_length=255)

    @field_validator("monthly_cost", mode="before")
    @classmethod
    def valid_cost(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = _decimal(value, "monthly_cost")
        if (
            parsed < 0
            or parsed > Decimal("99999999.99")
            or parsed.as_tuple().exponent < -2
        ):
            raise ValueError(
                "monthly_cost must be non-negative with at most 2 decimals"
            )
        return parsed

    @field_validator("placement_weight", mode="before")
    @classmethod
    def valid_weight(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = _decimal(value, "placement_weight")
        if parsed <= 0 or parsed > 1_000 or parsed.as_tuple().exponent < -3:
            raise ValueError(
                "placement_weight must be positive with at most 3 decimals"
            )
        return parsed

    @field_validator("api_key")
    @classmethod
    def valid_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\r" in value or "\n" in value or not value.strip():
            raise ValueError("api_key is invalid")
        return value.strip()

    @field_validator("name", "ip", "host", "location")
    @classmethod
    def optional_required_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator("ip")
    @classmethod
    def valid_manager_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(character.isspace() for character in value) or any(
            marker in value for marker in ("://", "/", "?", "#", "@")
        ):
            raise ValueError("ip must be a Manager hostname or address without a port")
        return value

    @field_validator("provider", "public_endpoint")
    @classmethod
    def optional_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


class AdminServerActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "health_check",
        "refresh_health",
        "refresh_status",
        "refresh_inventory",
        "audit_drift",
        "set_accepting",
        "enable_new_configs",
        "disable_new_configs",
        "drain",
        "start_drain",
        "disable",
        "activate",
        "retire",
        "update_capacity",
    ]
    reason: str = Field(default="admin console action", min_length=3, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)
    accepts_new_configs: bool | None = None
    max_configs: int | None = Field(default=None, ge=1, le=2_147_483_647)
    capacity: int | None = Field(default=None, ge=1, le=2_147_483_647)
    clear_max_configs: bool = False
    capacity_reserve: int | None = Field(default=None, ge=0, le=2_147_483_647)
    placement_weight: Decimal | None = None

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("placement_weight", mode="before")
    @classmethod
    def valid_weight(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        parsed = _decimal(value, "placement_weight")
        if parsed <= 0 or parsed > 1_000 or parsed.as_tuple().exponent < -3:
            raise ValueError(
                "placement_weight must be positive with at most 3 decimals"
            )
        return parsed

    @model_validator(mode="after")
    def normalize_capacity(self):
        if self.capacity is not None and self.max_configs is not None:
            raise ValueError("use either capacity or max_configs")
        return self
