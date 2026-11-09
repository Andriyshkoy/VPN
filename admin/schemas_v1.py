from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.exceptions import InvalidOperationError
from core.services.node_policy import normalize_node_policy

MONEY_QUANTUM = Decimal("0.01")


def _strict_money(value, *, positive: bool) -> Decimal:
    if not isinstance(value, str):
        raise ValueError("Money values must be JSON strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid money value") from exc
    if not parsed.is_finite() or parsed.as_tuple().exponent < -2:
        raise ValueError("Money values support at most two decimal places")
    parsed = parsed.quantize(MONEY_QUANTUM, ROUND_HALF_UP)
    if positive and parsed <= 0:
        raise ValueError("Amount must be positive")
    if abs(parsed) > Decimal("9999999999999999.99"):
        raise ValueError("Money value is too large")
    return parsed


class BalanceAdjustmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: Literal["credit", "debit"]
    amount: Decimal
    reason_code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_:-]+$")
    comment: str = Field(min_length=3, max_length=500)
    expected_balance: Decimal | None = None
    expected_ledger_entry_id: int | None = Field(default=None, ge=0)

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(cls, value):
        return _strict_money(value, positive=True)

    @field_validator("expected_balance", mode="before")
    @classmethod
    def validate_expected_balance(cls, value):
        if value is None:
            return None
        return _strict_money(value, positive=False)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 3:
            raise ValueError("Comment is too short")
        return normalized


class ServerActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "refresh_health",
        "refresh_inventory",
        "audit_drift",
        "enable_new_configs",
        "disable_new_configs",
        "start_drain",
        "disable",
        "activate",
    ]
    reason: str = Field(min_length=3, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return " ".join(value.split())


class ConfigActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["suspend", "unsuspend", "revoke"]
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_config_reason(cls, value: str) -> str:
        return " ".join(value.split())


class NodeAccessPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    traffic_limit_bytes: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    expires_at: datetime | None = None
    activation_days: int | None = Field(default=None, ge=1, le=3660, strict=True)

    @field_validator("expires_at", mode="before")
    @classmethod
    def require_timestamp_text(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Expiry must be an aware timestamp")
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("Expiry must be an ISO timestamp") from exc
        return value

    @field_validator("expires_at")
    @classmethod
    def require_timezone(cls, value):
        if value is not None:
            if value.utcoffset() is None:
                raise ValueError("Expiry must include a timezone")
            return value.astimezone(timezone.utc)
        return value


class NodeConfigCreateRequest(BaseModel):
    """Operator technical provisioning, independent from customer checkout."""

    model_config = ConfigDict(extra="forbid")

    server_id: int = Field(ge=1, strict=True)
    owner_id: int = Field(ge=1, strict=True)
    name: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    service_id: Literal["hysteria", "vless-reality"]
    access_policy: NodeAccessPolicyRequest = Field(
        default_factory=NodeAccessPolicyRequest
    )
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("name", "display_name", "reason")
    @classmethod
    def trim_text(cls, value):
        result = " ".join(value.split())
        if not result:
            raise ValueError("Text cannot be blank")
        return result

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value) < 3:
            raise ValueError("Reason is too short")
        return value

    @model_validator(mode="after")
    def validate_service_policy(self):
        try:
            normalize_node_policy(self.service_id, self.access_policy.model_dump())
        except InvalidOperationError as exc:
            raise ValueError(str(exc)) from exc
        return self


class NodeProfileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_id: int
    access_id: str
    service_id: Literal["hysteria", "vless-reality"]
    format: Literal["uri"] = "uri"
    uri: str = Field(repr=False)
    contains_secret: Literal[True] = True
