"""Strict request contracts for subscriptions and operator campaigns."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 3:
            raise ValueError("Reason is too short")
        return value


class UserFlagRequest(ReasonRequest):
    enabled: bool = Field(strict=True)
    expected_enabled: bool = Field(strict=True)


class SubscriptionPlanRequest(ReasonRequest):
    plan_code: Literal["basic", "plus"]
    expected_version: int | None = Field(default=None, ge=1, strict=True)


class CampaignAudience(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["all", "selected"]
    user_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(
        default_factory=list, max_length=10000
    )

    @field_validator("user_ids")
    @classmethod
    def positive_unique_ids(cls, value: list[int]) -> list[int]:
        if any(isinstance(item, bool) or item <= 0 for item in value):
            raise ValueError("User IDs must be positive integers")
        if len(value) != len(set(value)):
            raise ValueError("Duplicate user IDs are not allowed")
        return sorted(value)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.kind == "all" and self.user_ids:
            raise ValueError("All-user audience cannot contain selected IDs")
        if self.kind == "selected" and not self.user_ids:
            raise ValueError("Select at least one user")
        return self


class CampaignDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4096)
    audience: CampaignAudience

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Name cannot be blank")
        return value

    @field_validator("text")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value


class CampaignUpdateRequest(CampaignDraftRequest):
    revision: int = Field(ge=1, strict=True)


class CampaignLaunchRequest(ReasonRequest):
    revision: int = Field(ge=1, strict=True)
    preview_token: str = Field(min_length=16, max_length=1024)
    expected_recipient_count: int = Field(ge=1, strict=True)
    confirmation: Literal["SEND"]
