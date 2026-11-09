from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ServiceId = Literal["hysteria", "vless-reality"]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccessCreate(RequestModel):
    service_id: ServiceId
    node_id: str | None = Field(default=None, min_length=1, max_length=63, pattern=r"^[a-z0-9][a-z0-9-]*$")
    owner_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_@.:/-]+$")
    traffic_limit_bytes: int = Field(default=0, ge=0, le=2**63 - 1, strict=True)
    expires_at: datetime | None = None
    activation_days: int | None = Field(default=None, ge=1, le=3660, strict=True)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("expires_at_requires_timezone")
            value = value.astimezone(timezone.utc)
        return value

    @model_validator(mode="after")
    def service_policy(self):
        if self.service_id == "hysteria" and self.expires_at is not None:
            raise ValueError("hysteria_uses_activation_days_not_exact_expiry")
        if self.service_id == "vless-reality" and self.activation_days is not None:
            raise ValueError("vless_uses_exact_expiry_not_activation_days")
        return self


class AccessUpdate(RequestModel):
    traffic_limit_bytes: int | None = Field(default=None, ge=0, le=2**63 - 1, strict=True)
    expires_at: datetime | None = None
    activation_days: int | None = Field(default=None, ge=1, le=3660, strict=True)
    owner_ref: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_@.:/-]+$")

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        return AccessCreate.aware_expiry(value)

    @model_validator(mode="after")
    def has_update(self):
        if not self.model_fields_set:
            raise ValueError("empty_update")
        if "traffic_limit_bytes" in self.model_fields_set and self.traffic_limit_bytes is None:
            raise ValueError("traffic_limit_bytes_cannot_be_null")
        return self


class EffectiveAccess(RequestModel):
    status: Literal["active", "suspended", "expired", "limited", "revoked"]
    used_traffic_bytes: int = Field(default=0, ge=0)
    traffic_limit_bytes: int = Field(default=0, ge=0)
    expires_at: datetime | None = None
    last_seen_at: datetime | None = None
    expiry_precision: Literal["second", "day"] = "second"
    accounting: str = "delayed_backend_counters"
    activation_days: int | None = None


class AccessSettings(RequestModel):
    traffic_limit_bytes: int
    expires_at: datetime | None
    activation_days: int | None


class AccessView(RequestModel):
    id: str
    service_id: ServiceId
    node_id: str
    owner_ref: str | None
    status: Literal["active", "suspended", "revoked", "expired", "limited", "provisioning", "failed"]
    desired_status: Literal["active", "suspended", "revoked"]
    settings: AccessSettings
    effective: EffectiveAccess | None
    revision: int
    created_at: datetime
    updated_at: datetime
    operation_pending: bool
    control_plane_verified: bool
    data_plane_verified: Literal[False]


class AccessResponse(RequestModel):
    access: AccessView


class MutationResponse(AccessResponse):
    operation_id: str


class AccessListResponse(RequestModel):
    accesses: list[AccessView]
    total: int
    limit: int
    offset: int
    observation_scope: str


class ProfileResponse(RequestModel):
    access_id: str
    service_id: ServiceId
    format: Literal["uri"]
    uri: str
    contains_secret: Literal[True]


class UsageResponse(RequestModel):
    access_id: str
    used_traffic_bytes: int
    traffic_limit_bytes: int
    accounting: str
    observed_at: datetime
    realtime: Literal[False]
    billing_authoritative: Literal[False]


class ErrorDetail(RequestModel):
    code: str
    request_id: str
    fields: list[dict] | None = None


class ErrorResponse(RequestModel):
    error: ErrorDetail
