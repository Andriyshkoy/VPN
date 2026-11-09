"""Marzban v0.8.4 adapter for dedicated VLESS + REALITY + Vision accounts.

Only marked control-api accounts are addressable. Native receiver configuration,
legacy users, credentials, UUIDs and traffic counters are never reset here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import re
import time
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

from .common import BackendAccess, BackendConflict, BackendNotFound, BackendUnavailable
from .config import private_json
from .vendor import marzban_manage as legacy


REFERENCE = re.compile(r"vc_[a-f0-9]{24}\Z")
SERVER = "195.133.195.40"
SNI = "hst.andriyshkoy.ru"
PORT = 9443


def _safe_host(value):
    if not isinstance(value, str) or not value or len(value) > 253 or value != value.strip():
        raise ValueError("invalid_public_host")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        labels = value.split(".")
        if len(labels) < 2 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", x)
                                      for x in labels):
            raise ValueError("invalid_public_host") from None
        return value.lower()


def _translate(error: legacy.OperatorError):
    if error.http_status == 404 or error.code == "user_not_found":
        return BackendNotFound("access_not_found")
    if error.code in {
        "existing_user_configuration_conflict", "existing_user_limits_differ_no_changes_made",
        "resume_blocked_by_expiry_or_traffic_limit", "export_requires_active_user",
        "user_not_assigned_to_managed_inbound",
    } or error.http_status in (400, 409, 422):
        return BackendConflict("backend_state_conflict")
    return BackendUnavailable("marzban_unavailable")


def _number(value, *, null=0):
    if value is None:
        return null
    if type(value) is not int or value < 0:
        raise BackendUnavailable("invalid_backend_response")
    return value


def _last_seen(value):
    if value is None:
        return None
    if type(value) is int and value >= 0:
        return value
    if not isinstance(value, str) or len(value) > 64:
        raise BackendUnavailable("invalid_backend_response")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        result = int(stamp.timestamp())
        if result < 0:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise BackendUnavailable("invalid_backend_response") from None


class MarzbanBackend:
    def __init__(self, credentials_path: Path | str = legacy.DEFAULT_CREDENTIALS,
                 *, public_host: str = SERVER, public_port: int = PORT,
                 reality_sni: str = SNI, api_factory: Callable[[], legacy.API] | None = None):
        self.credentials_path = Path(credentials_path)
        self.public_host = _safe_host(public_host)
        self.reality_sni = _safe_host(reality_sni)
        if type(public_port) is not int or not 1 <= public_port <= 65535:
            raise ValueError("invalid_public_port")
        self.public_port = public_port
        # API instances hold only short-lived in-memory tokens; do not share them
        # between threaded requests or persist them in the controller database.
        self._factory = api_factory or self._new_api

    def _new_api(self):
        try:
            source = private_json(self.credentials_path)
            required = ("api_url", "username", "password", "inbound_tag")
            if any(not isinstance(source.get(key), str) or not source[key] for key in required):
                raise ValueError
            credentials = {key: source[key] for key in required}
            if (credentials["inbound_tag"] != "VLESS_REALITY_NL"
                    or any(ch.isspace() for ch in credentials["api_url"])):
                raise ValueError
            location = urlsplit(credentials["api_url"])
            if (location.scheme != "http" or location.hostname not in ("127.0.0.1", "::1")
                    or location.username is not None or location.password is not None
                    or location.path not in ("", "/") or location.query or location.fragment
                    or location.port is None or not 1 <= location.port <= 65535):
                raise ValueError
            credentials["api_url"] = credentials["api_url"].rstrip("/")
            return legacy.API(credentials)
        except (OSError, ValueError, TypeError):
            # LoadCredential exposes a read-only 0400 file in a 0500 directory;
            # the standalone legacy CLI requires 0600/0700 and cannot load it.
            # Keep API-boundary errors fixed and free of file/secret contents.
            raise BackendUnavailable("marzban_credentials_unavailable") from None

    @staticmethod
    def _reference(username):
        if not isinstance(username, str) or not REFERENCE.fullmatch(username):
            raise BackendConflict("backend_reference_invalid")
        return username

    @staticmethod
    def _limits(expires_at, traffic_limit_bytes, activation_days):
        if activation_days is not None:
            raise BackendConflict("activation_days_unsupported")
        if expires_at is not None and (type(expires_at) is not int or expires_at <= 0):
            raise BackendConflict("invalid_backend_expiry")
        if type(traffic_limit_bytes) is not int or traffic_limit_bytes < 0:
            raise BackendConflict("invalid_backend_traffic_limit")
        return expires_at or 0, traffic_limit_bytes

    @staticmethod
    def _owned(user, username, inbound):
        if user is None:
            raise BackendNotFound("access_not_found")
        if user.get("note") != "vpn-control:" + username:
            raise BackendConflict("backend_ownership_conflict")
        try:
            proxies = user["proxies"]
            proxy = proxies["vless"]
            if (set(proxies) != {"vless"} or proxy.get("flow") != "xtls-rprx-vision"
                    or user["inbounds"].get("vless") != [inbound]
                    or user.get("data_limit_reset_strategy", "no_reset") != "no_reset"):
                raise BackendConflict("backend_configuration_conflict")
            UUID(proxy["id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BackendUnavailable("invalid_backend_response") from None
        return user

    @staticmethod
    def _snapshot(user):
        status = {"active": "active", "disabled": "suspended", "on_hold": "suspended",
                  "expired": "expired", "limited": "limited"}.get(user.get("status"))
        if status is None:
            raise BackendUnavailable("invalid_backend_response")
        used = _number(user.get("used_traffic"))
        limit = _number(user.get("data_limit"))
        expiry = _number(user.get("expire")) or None
        if status == "active":
            if expiry is not None and expiry <= int(time.time()):
                status = "expired"
            elif limit and used >= limit:
                status = "limited"
        return BackendAccess(status=status, used_traffic_bytes=used,
                             traffic_limit_bytes=limit, expires_at=expiry,
                             last_seen_at=_last_seen(user.get("online_at")),
                             expiry_precision="second",
                             accounting="delayed_backend_counters")

    def _connection(self):
        api = self._factory()
        return api, legacy.Operator(api)

    def get(self, username):
        self._reference(username)
        try:
            api, op = self._connection()
            user = api.user(username)
            if user is None:
                return None
            return self._snapshot(self._owned(user, username, op.inbound_tag))
        except legacy.OperatorError as error:
            raise _translate(error) from None

    def create(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        self._reference(username)
        expires, limit = self._limits(expires_at, traffic_limit_bytes, activation_days)
        try:
            api, op = self._connection()
            user = api.user(username)
            if user is None:
                payload = {"username": username, "status": "active", "expire": expires,
                           "data_limit": limit, "data_limit_reset_strategy": "no_reset",
                           "note": "vpn-control:" + username,
                           "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                           "inbounds": {"vless": [op.inbound_tag]}}
                try:
                    api.request("POST", "/api/user", payload)
                except legacy.OperatorError as error:
                    # A timed-out create may already be durable upstream. Re-read
                    # the deterministic username; never issue a second identity.
                    if error.http_status != 409 and error.code != "api_connection_failed":
                        raise
                    user = api.user(username)
                    if user is None:
                        raise error
                if user is None:
                    user = op.wait_user(username)
            user = self._owned(user, username, op.inbound_tag)
            op.check_existing_create(user, expires, limit)
            return self._snapshot(user)
        except legacy.OperatorError as error:
            raise _translate(error) from None

    def update(self, username, *, expires_at, traffic_limit_bytes, activation_days=None):
        self._reference(username)
        expires, limit = self._limits(expires_at, traffic_limit_bytes, activation_days)
        try:
            api, op = self._connection()
            user = self._owned(api.user(username), username, op.inbound_tag)
            original_identity = user["proxies"]["vless"]["id"]
            if (user.get("expire") or 0) != expires or (user.get("data_limit") or 0) != limit:
                ambiguous = False
                try:
                    api.request("PUT", "/api/user/" + quote(username, safe=""),
                                {"expire": expires, "data_limit": limit,
                                 "data_limit_reset_strategy": "no_reset"})
                except legacy.OperatorError as error:
                    if error.code != "api_connection_failed":
                        raise
                    # Reconcile an ambiguous PUT through the same readback below.
                    ambiguous = True
                user = self._owned(op.wait_user(username), username, op.inbound_tag)
                if ambiguous and ((user.get("expire") or 0) != expires
                                  or (user.get("data_limit") or 0) != limit):
                    raise BackendUnavailable("backend_mutation_outcome_unknown")
            op.check_existing_create(user, expires, limit)
            if user["proxies"]["vless"]["id"] != original_identity:
                raise BackendUnavailable("backend_identity_changed")
            return self._snapshot(user)
        except legacy.OperatorError as error:
            raise _translate(error) from None

    def set_status(self, username, status):
        self._reference(username)
        if status not in {"active", "suspended"}:
            raise BackendConflict("backend_status_unsupported")
        try:
            api, op = self._connection()
            self._owned(api.user(username), username, op.inbound_tag)
            op.set_status(username, "active" if status == "active" else "disabled")
            return self._snapshot(self._owned(api.user(username), username, op.inbound_tag))
        except legacy.OperatorError as error:
            raise _translate(error) from None

    def revoke(self, username):
        self._reference(username)
        try:
            api, op = self._connection()
            user = api.user(username)
            if user is None:
                return
            self._owned(user, username, op.inbound_tag)
            try:
                op.revoke(username)
            except legacy.OperatorError as error:
                if error.code != "api_connection_failed" or api.user(username) is not None:
                    raise
        except legacy.OperatorError as error:
            raise _translate(error) from None

    def profile(self, username):
        self._reference(username)
        try:
            api, op = self._connection()
            user = self._owned(api.user(username), username, op.inbound_tag)
            if self._snapshot(user).status != "active":
                raise BackendConflict("profile_requires_active_access")
            identity = str(UUID(user["proxies"]["vless"]["id"]))
            links = user.get("links")
            if not isinstance(links, list):
                raise BackendUnavailable("invalid_backend_profile")
            selected = []
            for link in links:
                if not isinstance(link, str) or len(link) > 8192 or any(ch.isspace() for ch in link):
                    continue
                parsed = urlsplit(link)
                query = parse_qs(parsed.query)
                if (parsed.scheme == "vless" and _safe_host(parsed.hostname) == self.public_host
                        and parsed.port == self.public_port
                        and parsed.username == identity
                        and query.get("security") == ["reality"]
                        and query.get("encryption", ["none"]) == ["none"]
                        and query.get("type") in (["tcp"], ["raw"])
                        and query.get("flow") == ["xtls-rprx-vision"]
                        and query.get("sni") == [self.reality_sni]
                        and all(len(query.get(key, [])) == 1 and query[key][0]
                                for key in ("pbk", "sid", "fp"))):
                    selected.append(link)
            if len(selected) != 1:
                raise BackendUnavailable("invalid_backend_profile")
            return selected[0]
        except legacy.OperatorError as error:
            raise _translate(error) from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise BackendUnavailable("invalid_backend_profile") from None

    def health(self):
        try:
            _, op = self._connection()
            status = op.status()
            return {"ready": status["ready"], "engine": "xray",
                    "engine_version": status["core_version"], "manager": "marzban",
                    "scope": "authenticated_control_api_and_reported_engine_process",
                    "data_plane_verified": False}
        except legacy.OperatorError as error:
            raise _translate(error) from None
