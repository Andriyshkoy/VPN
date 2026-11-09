"""Loopback node agent. Business balances and payments belong to the platform."""
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import __version__
from .body_limit import BodyLimitMiddleware
from .common import BackendConflict, BackendError, BackendNotFound, BackendUnavailable
from .config import Principal, Settings
from .models import (AccessCreate, AccessUpdate, ServiceId, AccessResponse, MutationResponse,
                     AccessListResponse, ProfileResponse, UsageResponse, ErrorResponse)
from .store import Store, StoreError

LOGGER = logging.getLogger("vpn_control")
BEARER = HTTPBearer(auto_error=False, description="Operator token; scopes: access:read, access:write, profiles:read")
IDEMPOTENCY_OPENAPI = {"x-required-scopes": ["access:write"], "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True,
    "description": "Durable per-principal key. Reuse the identical key, method, path and body after a timeout. Never reuse a key for another operation.",
    "schema": {"type": "string", "minLength": 8, "maxLength": 128, "pattern": "^[A-Za-z0-9_.:-]+$"}}]}
CATALOG = {
    "hysteria": {"id": "hysteria", "name": "Hysteria 2", "node_id": "nl-1", "endpoint": {"host": "195.133.195.40", "port": 1935, "transport": "udp"},
                 "capabilities": {"fixed_expiry": False, "activation_days": True, "expiry_precision": "day", "traffic_limit_bytes": True, "unlimited_traffic": True, "unlimited_expiry": True, "max_sessions": False},
                 "accounting": "delayed_total_upload_plus_download", "scope": "control_plane; client/network acceptance is separate"},
    "vless-reality": {"id": "vless-reality", "name": "VLESS + REALITY + Vision", "node_id": "nl-1", "endpoint": {"host": "195.133.195.40", "port": 9443, "transport": "tcp"},
                      "capabilities": {"fixed_expiry": True, "activation_days": False, "expiry_precision": "second", "traffic_limit_bytes": True, "unlimited_traffic": True, "unlimited_expiry": True, "max_sessions": False},
                      "accounting": "delayed_backend_counters; Vision splice may settle after stream close", "scope": "control_plane; client/network acceptance is separate"},
}


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z") if timestamp is not None else None


def view(row, snapshot=None):
    observed = snapshot if snapshot is not None else row.get("snapshot")
    desired = dict(row["settings"])
    desired.setdefault("max_sessions", None)
    desired["expires_at"] = iso(desired.get("expires_at"))
    effective = dict(observed) if observed else None
    if effective:
        effective.setdefault("max_sessions", None)
        effective["expires_at"] = iso(effective.get("expires_at"))
        effective["last_seen_at"] = iso(effective.get("last_seen_at"))
    return {"id": row["id"], "service_id": row["service_id"], "node_id": row["node_id"], "owner_ref": row["owner_ref"],
            "status": observed["status"] if observed else row["status"], "desired_status": row["desired_status"],
            "settings": desired, "effective": effective, "revision": row["revision"],
            "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"]),
            "operation_pending": row["current_operation"] is not None,
            "control_plane_verified": observed is not None, "data_plane_verified": False}


def create_app(settings=None, backends=None):
    settings = settings or Settings.from_env()
    catalog = json.loads(json.dumps(CATALOG))
    for service in catalog.values():
        service["node_id"] = settings.node_id
        service["endpoint"]["host"] = settings.public_host
    catalog["hysteria"]["endpoint"]["port"] = settings.hysteria_port
    catalog["vless-reality"]["endpoint"]["port"] = settings.vless_port

    @asynccontextmanager
    async def lifespan(application):
        application.state.principals = settings.load_principals()
        application.state.store = Store(settings.database)
        if backends is None:
            from .marzban import MarzbanBackend
            from .hysteria import HysteriaBackend
            application.state.backends = {"vless-reality": MarzbanBackend(settings.marzban_credentials, public_host=settings.public_host, public_port=settings.vless_port, reality_sni=settings.reality_sni),
                                          "hysteria": HysteriaBackend(settings.hysteria_socket)}
        else:
            application.state.backends = backends
        if set(application.state.backends) != set(CATALOG):
            raise ValueError("both_service_backends_required")
        yield

    application = FastAPI(title="VPN Node Agent", version=__version__, lifespan=lifespan,
                          docs_url=None, redoc_url=None, openapi_url=None,
                          responses={status: {"model": ErrorResponse} for status in (400, 401, 403, 404, 409, 413, 422, 500, 503)},
                          description="Internal NL node control. No balances, payments or tariff pricing. Only accesses created by this agent are managed.")

    @application.middleware("http")
    async def headers(request, call_next):
        request.state.request_id = str(uuid4())
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 16384):
            response = JSONResponse({"error": {"code": "request_too_large", "request_id": request.state.request_id}}, status_code=413)
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                LOGGER.error("request_failed id=%s class=%s", request.state.request_id, type(exc).__name__)
                response = JSONResponse({"error": {"code": "internal_error", "request_id": request.state.request_id}}, status_code=500)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.exception_handler(APIError)
    async def api_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse({"error": {"code": exc.code, "request_id": request.state.request_id}}, status_code=exc.status, headers=headers)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        known = {"body", "path", "query", "header", "access_id", "service_id", "node_id", "owner_ref", "traffic_limit_bytes", "expires_at", "activation_days", "max_sessions", "limit", "offset"}
        fields = [{"location": [part if isinstance(part, int) or part in known else "unknown_field" for part in x["loc"]], "type": x["type"]} for x in exc.errors()]
        return JSONResponse({"error": {"code": "request_validation_failed", "request_id": request.state.request_id,
                            "fields": fields}}, status_code=422)

    def authenticate(request: Request, credential: HTTPAuthorizationCredentials | None = Depends(BEARER)):
        if credential is None or credential.scheme.lower() != "bearer" or not 32 <= len(credential.credentials) <= 512:
            raise APIError(401, "authentication_required")
        candidate = hashlib.sha256(credential.credentials.encode()).hexdigest()
        principal = None
        for item in request.app.state.principals:
            if hmac.compare_digest(candidate, item.sha256):
                principal = item
        if principal is None:
            raise APIError(401, "authentication_required")
        return principal

    def require(scope):
        def check(principal: Principal = Depends(authenticate)):
            if scope not in principal.scopes:
                raise APIError(403, "scope_required")
            return principal
        return check

    read = require("access:read")
    write = require("access:write")
    export = require("profiles:read")

    def backend(request, row):
        return request.app.state.backends[row["service_id"]]

    def required_row(request, access_id):
        row = request.app.state.store.get(str(access_id))
        if row is None:
            raise APIError(404, "access_not_found")
        return row

    def backend_error(error):
        code = error.code if re.fullmatch(r"[a-z0-9_]{1,100}", error.code) else "backend_error"
        if isinstance(error, BackendUnavailable):
            return 503, code
        if isinstance(error, BackendNotFound):
            return 503, "backend_record_missing"
        unsupported = "unsupported" in code or code.startswith("invalid_")
        return (422 if unsupported else 409), code

    def safe_call(action):
        try:
            return action()
        except BackendError as exc:
            status, code = backend_error(exc)
            raise APIError(status, code) from None

    def json_response(body, status=200, replay=False, request=None):
        if "error" in body:
            body = {"error": {**body["error"], "request_id": request.state.request_id}}
        return JSONResponse(body, status_code=status, headers={"Idempotency-Replayed": str(replay).lower()})

    def mutate(request, principal, kind, payload, access_id=None):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
            raise APIError(400, "idempotency_key_required")
        canonical = json.dumps({"method": request.method, "path": request.url.path, "payload": payload}, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        store = request.app.state.store
        try:
            existing = store.operation_exists(principal.id, key, request_hash)
            if not existing:
                timestamp = payload.get("expires_at")
                if timestamp is not None and timestamp <= time.time():
                    raise APIError(422, "expires_at_must_be_future")
            create = None
            if kind == "create":
                create = {"service_id": payload["service_id"], "node_id": payload["node_id"], "owner_ref": payload["owner_ref"],
                          "settings": {k: payload[k] for k in ("expires_at", "traffic_limit_bytes", "activation_days", "max_sessions")}}
            lease = store.reserve(principal.id, key, request_hash, kind, str(access_id) if access_id else None, create)
        except StoreError as exc:
            raise APIError(exc.status, exc.code) from None
        if lease["replay"]:
            return json_response(lease["response"], lease["http_status"], replay=True, request=request)
        row, operation_id, lease_owner = lease["access"], lease["operation_id"], lease["lease_owner"]
        adapter = backend(request, row)
        next_settings = dict(row["settings"])
        next_settings.setdefault("max_sessions", None)  # Existing durable rows are unlimited.
        next_owner, owner_changed = row["owner_ref"], False
        target = row["desired_status"]
        try:
            if kind == "create":
                result = adapter.create(row["backend_username"], **next_settings)
            elif kind == "update":
                if row["service_id"] == "hysteria" and "expires_at" in payload and payload["expires_at"] is not None:
                    raise BackendConflict("hysteria_exact_expiry_unsupported")
                if row["service_id"] == "vless-reality" and payload.get("activation_days") is not None:
                    raise BackendConflict("activation_days_unsupported")
                if row["service_id"] == "vless-reality" and payload.get("max_sessions") is not None:
                    raise BackendConflict("max_sessions_unsupported")
                next_settings.update({k: v for k, v in payload.items() if k in next_settings})
                if "owner_ref" in payload:
                    next_owner, owner_changed = payload["owner_ref"], True
                result = adapter.update(row["backend_username"], **next_settings)
            elif kind in ("suspend", "resume"):
                target = "suspended" if kind == "suspend" else "active"
                result = adapter.set_status(row["backend_username"], target)
            elif kind == "revoke":
                adapter.revoke(row["backend_username"])
                target = "revoked"
                result = None
            else:
                raise RuntimeError("unsupported_operation")
            snapshot = asdict(result) if result is not None else {**(row["snapshot"] or {}), "status": "revoked"}
            anticipated = {**row, "snapshot": snapshot, "settings": next_settings, "owner_ref": next_owner,
                           "status": snapshot["status"], "desired_status": target, "current_operation": None,
                           "revision": row["revision"] + 1, "updated_at": int(time.time())}
            response = {"access": view(anticipated), "operation_id": operation_id}
            status = 201 if kind == "create" else 200
            store.finish(operation_id, lease_owner, row["id"], response, status, snapshot=snapshot,
                         settings=next_settings, owner_ref=next_owner, owner_changed=owner_changed, desired_status=target)
            return json_response(response, status, request=request)
        except BackendUnavailable as exc:
            store.release(operation_id, lease_owner)
            raise APIError(503, backend_error(exc)[1]) from None
        except BackendError as exc:
            status, code = backend_error(exc)
            body = {"error": {"code": code}}
            store.finish(operation_id, lease_owner, row["id"], body, status, terminal_error=True)
            return json_response(body, status, request=request)
        except Exception:
            store.release(operation_id, lease_owner)
            raise

    @application.get("/healthz", tags=["health"])
    def healthz():
        return {"status": "ok", "version": __version__, "scope": "process_liveness"}

    @application.get("/openapi.json", include_in_schema=False)
    def openapi_json(principal=Depends(read)):
        return JSONResponse(application.openapi())

    @application.get("/v1/services", tags=["catalog"])
    def services(request: Request, principal=Depends(read)):
        observed = json.loads(json.dumps(catalog))
        try:
            capabilities = request.app.state.backends["hysteria"].health().get("capabilities", {})
            if (isinstance(capabilities, dict) and capabilities.get("max_sessions") is True
                    and capabilities.get("session_scope") == "auth_id_per_server_instance"
                    and capabilities.get("enforcement") == "atomic_quic_connection"
                    and type(capabilities.get("version")) is int and capabilities["version"] == 1):
                observed["hysteria"]["capabilities"].update(capabilities)
        except BackendError:
            pass
        return {"services": list(observed.values())}

    @application.get("/v1/nodes", tags=["catalog"])
    def nodes(principal=Depends(read)):
        return {"nodes": [{"id": settings.node_id, "country": settings.country_code, "services": list(catalog), "role": "vpn_node"}]}

    @application.get("/v1/health", tags=["health"])
    @application.get("/readyz", tags=["health"])
    def readiness(request: Request, principal=Depends(read)):
        results = {}
        try:
            with request.app.state.store.connect() as db:
                db.execute("SELECT COUNT(*) FROM operations").fetchone()
            database_ready = True
        except Exception:
            database_ready = False
        for service, adapter in request.app.state.backends.items():
            try:
                value = adapter.health()
                results[service] = value
            except BackendError:
                results[service] = {"ready": False, "error": "backend_unavailable"}
        ready = database_ready and all(x.get("ready") is True for x in results.values())
        return JSONResponse({"ready": ready, "database_ready": database_ready, "services": results, "scope": "control_plane; no end_to_end_probe"}, status_code=200 if ready else 503)

    @application.post("/v1/accesses", tags=["accesses"], status_code=201, response_model=MutationResponse, openapi_extra=IDEMPOTENCY_OPENAPI)
    def create_access(request: Request, body: AccessCreate, principal=Depends(write)):
        payload = body.model_dump(mode="json")
        if body.node_id is not None and body.node_id != settings.node_id:
            raise APIError(422, "node_id_not_local")
        payload["node_id"] = settings.node_id
        payload["expires_at"] = int(body.expires_at.timestamp()) if body.expires_at else None
        return mutate(request, principal, "create", payload)

    @application.get("/v1/accesses", tags=["accesses"], response_model=AccessListResponse)
    def list_accesses(request: Request, owner_ref: str | None = Query(None, max_length=128), service_id: ServiceId | None = None,
                      limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), principal=Depends(read)):
        rows, total = request.app.state.store.listing(owner_ref, service_id, limit, offset)
        return {"accesses": [view(x) for x in rows], "total": total, "limit": limit, "offset": offset,
                "observation_scope": "agent_registry_cached_state; GET an access or usage for fresh backend state"}

    @application.get("/v1/accesses/{access_id}", tags=["accesses"], response_model=AccessResponse)
    def get_access(request: Request, access_id: UUID, principal=Depends(read)):
        row = required_row(request, access_id)
        if row["status"] == "revoked":
            return {"access": view(row)}
        observed = safe_call(lambda: backend(request, row).get(row["backend_username"]))
        if observed is None and row["status"] != "provisioning":
            raise APIError(503, "backend_record_missing")
        return {"access": view(row, asdict(observed) if observed else None)}

    @application.patch("/v1/accesses/{access_id}", tags=["accesses"], response_model=MutationResponse, openapi_extra=IDEMPOTENCY_OPENAPI)
    def update_access(request: Request, access_id: UUID, body: AccessUpdate, principal=Depends(write)):
        payload = body.model_dump(mode="json", exclude_unset=True)
        if "expires_at" in payload:
            payload["expires_at"] = int(body.expires_at.timestamp()) if body.expires_at else None
        return mutate(request, principal, "update", payload, access_id)

    @application.post("/v1/accesses/{access_id}/suspend", tags=["accesses"], response_model=MutationResponse, openapi_extra=IDEMPOTENCY_OPENAPI)
    def suspend_access(request: Request, access_id: UUID, principal=Depends(write)):
        return mutate(request, principal, "suspend", {}, access_id)

    @application.post("/v1/accesses/{access_id}/resume", tags=["accesses"], response_model=MutationResponse, openapi_extra=IDEMPOTENCY_OPENAPI)
    def resume_access(request: Request, access_id: UUID, principal=Depends(write)):
        return mutate(request, principal, "resume", {}, access_id)

    @application.delete("/v1/accesses/{access_id}", tags=["accesses"], response_model=MutationResponse, openapi_extra=IDEMPOTENCY_OPENAPI)
    def revoke_access(request: Request, access_id: UUID, principal=Depends(write)):
        return mutate(request, principal, "revoke", {}, access_id)

    @application.get("/v1/accesses/{access_id}/profile", tags=["profiles"], response_model=ProfileResponse, openapi_extra={"x-required-scopes": ["profiles:read"]})
    def profile(request: Request, access_id: UUID, profile_format: Literal["auto", "native-uri"] = Query(default="auto", alias="format"), principal=Depends(export)):
        row = required_row(request, access_id)
        if row["status"] in ("revoked", "provisioning", "failed"):
            raise APIError(409, "profile_unavailable_for_state")
        selected = backend(request, row)
        if row["service_id"] == "hysteria" and profile_format == "auto":
            content = safe_call(lambda: selected.profile_json(row["backend_username"]))
            return {"access_id": row["id"], "service_id": row["service_id"], "format": "singbox-json", "uri": None, "content": content, "contains_secret": True}
        uri = safe_call(lambda: selected.profile(row["backend_username"]))
        return {"access_id": row["id"], "service_id": row["service_id"], "format": "uri", "uri": uri, "content": None, "contains_secret": True}

    @application.get("/v1/accesses/{access_id}/usage", tags=["accesses"], response_model=UsageResponse)
    def usage(request: Request, access_id: UUID, principal=Depends(read)):
        row = required_row(request, access_id)
        observed = row["snapshot"] if row["status"] == "revoked" else None
        if observed is None:
            current = safe_call(lambda: backend(request, row).get(row["backend_username"]))
            if current is None:
                raise APIError(503, "backend_record_missing")
            observed = asdict(current)
        return {"access_id": row["id"], "used_traffic_bytes": observed.get("used_traffic_bytes", 0),
                "traffic_limit_bytes": observed.get("traffic_limit_bytes", 0), "accounting": observed.get("accounting", "last_observed_before_revocation"),
                "observed_at": iso(int(time.time())) if row["status"] != "revoked" else iso(row["updated_at"]),
                "realtime": False, "billing_authoritative": False}

    application.add_middleware(BodyLimitMiddleware, limit=16384)
    return application


app = create_app()
