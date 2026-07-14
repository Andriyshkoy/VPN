from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
