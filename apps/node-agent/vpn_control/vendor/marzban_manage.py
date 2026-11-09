#!/usr/bin/env python3
"""Private operator commands for the existing Marzban v0.8.4 API.

Run on the manager host. Credentials stay in a root-owned private file, tokens
stay in memory, and access links are written only by the explicit export command.
Control-plane convergence does not prove that Xray applied a background task.

API schema: https://github.com/Gozargah/Marzban/tree/v0.8.4/app/routers
"""
import argparse
import base64
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID


DEFAULT_CREDENTIALS = Path("/opt/vpn-managed/secrets/operator.json")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 5
STATUSES = {"active", "disabled", "limited", "expired", "on_hold"}
USERNAME = re.compile(r"[A-Za-z0-9_@.-]{3,32}\Z")


class OperatorError(Exception):
    """Only fixed, non-secret identifiers are carried into terminal output."""

    def __init__(self, code, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)

    def as_dict(self):
        result = {"error": self.code}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_name(name):
    if not isinstance(name, str) or not USERNAME.fullmatch(name):
        raise OperatorError("invalid_technical_username")
    return name


def load_credentials(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            parent = Path(path).parent.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise OperatorError("credentials_must_be_owned_and_private")
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise OperatorError("invalid_credentials_file")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        for key in ("api_url", "username", "password", "inbound_tag"):
            if not isinstance(result.get(key), str) or not result[key]:
                raise ValueError
        location = urlsplit(result["api_url"])
        if (location.scheme != "http" or location.hostname not in ("127.0.0.1", "::1")
                or location.username is not None or location.password is not None
                or location.path not in ("", "/") or location.query or location.fragment
                or location.port is None or not 1 <= location.port <= 65535):
            raise OperatorError("api_must_be_explicit_loopback_http")
        result["api_url"] = result["api_url"].rstrip("/")
        return result
    except OperatorError:
        raise
    except (OSError, ValueError, TypeError):
        raise OperatorError("invalid_credentials_file") from None


class API:
    def __init__(self, credentials):
        self.credentials = credentials
        self.token = None
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def request(self, method, path, payload=None, *, form=False, authenticated=True):
        if not path.startswith("/api/") or "\n" in path or "\r" in path:
            raise OperatorError("invalid_api_path")
        if authenticated and self.token is None:
            self.login()
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self.token
        body = None
        if payload is not None:
            if form:
                body = urlencode(payload).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
        request = Request(self.credentials["api_url"] + path, data=body,
                          headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OperatorError("api_response_too_large")
            return json.loads(raw) if raw else None
        except HTTPError as error:
            code = "authentication_failed" if error.code in (401, 403) else "api_http_error"
            # Never copy response bodies, request headers or URLs into an error.
            raise OperatorError(code, error.code) from None
        except (URLError, OSError, TimeoutError, socket.timeout):
            raise OperatorError("api_connection_failed") from None
        except (ValueError, UnicodeError):
            raise OperatorError("invalid_api_response") from None

    def login(self):
        data = self.request("POST", "/api/admin/token", {
            "username": self.credentials["username"],
            "password": self.credentials["password"]}, form=True, authenticated=False)
        try:
            token = data["access_token"]
            if not isinstance(token, str) or len(token) > 16384:
                raise ValueError
            parts = token.split(".")
            if len(parts) != 3:
                raise ValueError
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            expiry = payload.get("exp")
            if (isinstance(expiry, bool) or not isinstance(expiry, (float, int))
                    or not math.isfinite(expiry) or expiry <= time.time()):
                raise OperatorError("admin_token_requires_future_expiry")
        except OperatorError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise OperatorError("invalid_authentication_response") from None
        self.token = token

    def user(self, name):
        try:
            result = self.request("GET", "/api/user/" + quote(validate_name(name), safe=""))
        except OperatorError as error:
            if error.http_status == 404:
                return None
            raise
        if not isinstance(result, dict) or result.get("username") != name:
            raise OperatorError("invalid_api_response")
        return result


def numeric(value):
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise OperatorError("invalid_api_response")
    return value


def safe_user(user, inbound_tag):
    if not isinstance(user, dict):
        raise OperatorError("invalid_api_response")
    name = validate_name(user.get("username"))
    status = user.get("status")
    if status not in STATUSES:
        raise OperatorError("invalid_api_response")
    inbounds = user.get("inbounds", {})
    protocols = user.get("proxies", {})
    if not isinstance(inbounds, dict) or not isinstance(protocols, dict):
        raise OperatorError("invalid_api_response")
    return {"username": name, "status": status,
            **{key: numeric(user.get(key)) for key in
               ("expire", "data_limit", "used_traffic", "lifetime_used_traffic")},
            "vless_enabled": "vless" in protocols,
            "selected_inbound": inbound_tag in inbounds.get("vless", [])}


class Operator:
    def __init__(self, api):
        self.api = api
        self.inbound_tag = api.credentials["inbound_tag"]

    def required_user(self, name):
        result = self.api.user(name)
        if result is None:
            raise OperatorError("user_not_found", 404)
        return result

    def wait_user(self, name, expected_status=None, *, absent=False):
        deadline = time.monotonic() + 3
        while True:
            user = self.api.user(name)
            if absent and user is None:
                return None
            if not absent and user is not None and (expected_status is None or user.get("status") == expected_status):
                return user
            if time.monotonic() >= deadline:
                raise OperatorError("control_plane_did_not_converge")
            time.sleep(0.15)

    def result(self, action, user, changed):
        return {"action": action, "changed": changed,
                "control_plane_verified": True, "data_plane_verified": False,
                "user": safe_user(user, self.inbound_tag)}

    def status(self):
        # started is Marzban's process state; it is not an end-to-end VPN probe.
        admin = self.api.request("GET", "/api/admin")
        if not isinstance(admin, dict) or not isinstance(admin.get("is_sudo"), bool):
            raise OperatorError("invalid_api_response")
        core = self.api.request("GET", "/api/core")
        if (not isinstance(core, dict) or type(core.get("started")) is not bool
                or not isinstance(core.get("version"), str)
                or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", core["version"])):
            raise OperatorError("invalid_api_response")
        system = {}
        try:
            source = self.api.request("GET", "/api/system")
        except OperatorError as error:
            if error.http_status not in (404, 405):
                raise
        else:
            if not isinstance(source, dict):
                raise OperatorError("invalid_api_response")
            for key in ("mem_total", "mem_used", "cpu_cores", "total_user", "online_users",
                        "users_active", "users_on_hold", "users_disabled", "users_expired",
                        "users_limited", "incoming_bandwidth", "outgoing_bandwidth",
                        "incoming_bandwidth_speed", "outgoing_bandwidth_speed"):
                if key in source:
                    system[key] = numeric(source[key])
        return {"api_reachable": True, "authenticated": True,
                "administrator": admin["is_sudo"], "core_started": core["started"],
                "core_version": core["version"], "ready": core["started"],
                "readiness_scope": "authenticated API and reported Xray process state",
                "system": system, "data_plane_verified": False}

    def listing(self):
        users = []
        offset = 0
        while True:
            page = self.api.request("GET", "/api/users?" + urlencode({"offset": offset, "limit": 100}))
            if (not isinstance(page, dict) or not isinstance(page.get("users"), list)
                    or type(page.get("total")) is not int or not 0 <= page["total"] <= 10000):
                raise OperatorError("invalid_api_response")
            users.extend(safe_user(user, self.inbound_tag) for user in page["users"])
            offset += len(page["users"])
            if not page["users"] or offset >= page["total"]:
                return {"users": users, "total": page["total"], "returned": len(users)}
            if offset >= 10000:
                raise OperatorError("api_inventory_limit_exceeded")

    def show(self, name):
        return safe_user(self.required_user(name), self.inbound_tag)

    def check_existing_create(self, user, expires, limit_bytes):
        proxy = user.get("proxies", {}).get("vless", {})
        if (proxy.get("flow") != "xtls-rprx-vision"
                or self.inbound_tag not in user.get("inbounds", {}).get("vless", [])):
            raise OperatorError("existing_user_configuration_conflict")
        if (user.get("expire") or 0) != expires or (user.get("data_limit") or 0) != limit_bytes:
            raise OperatorError("existing_user_limits_differ_no_changes_made")

    def create(self, name, expires=0, limit_bytes=0):
        validate_name(name)
        if type(expires) is not int or type(limit_bytes) is not int or min(expires, limit_bytes) < 0:
            raise OperatorError("invalid_expiry_or_limit")
        user = self.api.user(name)
        if user is not None:
            self.check_existing_create(user, expires, limit_bytes)
            return self.result("existing", user, False)
        payload = {"username": name, "status": "active", "expire": expires,
                   "data_limit": limit_bytes, "data_limit_reset_strategy": "no_reset",
                   "proxies": {"vless": {"flow": "xtls-rprx-vision"}},
                   "inbounds": {"vless": [self.inbound_tag]}}
        changed = True
        try:
            self.api.request("POST", "/api/user", payload)
        except OperatorError as error:
            if error.http_status == 409:
                changed = False  # A concurrent/replayed create may already have committed.
            elif error.code == "api_connection_failed":
                user = self.api.user(name)
                if user is None:
                    raise error
                self.check_existing_create(user, expires, limit_bytes)
                return self.result("existing_after_ambiguous_create", user, False)
            else:
                raise
        user = self.wait_user(name)
        self.check_existing_create(user, expires, limit_bytes)
        return self.result("created" if changed else "existing", user, changed)

    def set_status(self, name, target):
        user = self.required_user(name)
        if user.get("status") == target:
            return self.result("unchanged", user, False)
        if target == "active":
            expires = user.get("expire") or 0
            limit = user.get("data_limit") or 0
            used = user.get("used_traffic") or 0
            if (expires and expires <= time.time()) or (limit and used >= limit):
                raise OperatorError("resume_blocked_by_expiry_or_traffic_limit")
        self.api.request("PUT", "/api/user/" + quote(validate_name(name), safe=""), {"status": target})
        return self.result("resumed" if target == "active" else "suspended",
                           self.wait_user(name, target), True)

    def revoke(self, name):
        validate_name(name)
        try:
            self.api.request("DELETE", "/api/user/" + quote(name, safe=""))
        except OperatorError as error:
            if error.http_status == 404:
                return {"action": "already_absent", "username": name, "changed": False,
                        "control_plane_verified": True, "data_plane_verified": False}
            raise
        self.wait_user(name, absent=True)
        return {"action": "revoked", "username": name, "changed": True,
                "control_plane_verified": True, "data_plane_verified": False}

    def export(self, name, output):
        user = self.required_user(name)
        if user.get("status") != "active":
            raise OperatorError("export_requires_active_user")
        if self.inbound_tag not in user.get("inbounds", {}).get("vless", []):
            raise OperatorError("user_not_assigned_to_managed_inbound")
        try:
            identity = str(UUID(user["proxies"]["vless"]["id"]))
            links = user["links"]
            if not isinstance(links, list):
                raise ValueError
            selected = []
            for link in links:
                if not isinstance(link, str) or any(ch.isspace() for ch in link):
                    continue
                parsed = urlsplit(link)
                query = parse_qs(parsed.query)
                if (parsed.scheme == "vless" and parsed.hostname == "195.133.195.40"
                        and parsed.port == 9443 and parsed.username == identity
                        and query.get("security") == ["reality"]
                        and query.get("encryption", ["none"]) == ["none"]
                        and query.get("type") in (["tcp"], ["raw"])
                        and query.get("flow") == ["xtls-rprx-vision"]
                        and all(len(query.get(key, [])) == 1 and query[key][0]
                                for key in ("pbk", "sid", "sni", "fp"))):
                    selected.append(link)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise OperatorError("invalid_export_configuration") from None
        if len(selected) != 1:
            raise OperatorError("export_requires_one_unambiguous_managed_link")
        write_private_export(output, (selected[0] + "\n").encode())
        return {"action": "exported", "username": name, "output": str(output),
                "format": "vless-uri", "contains_secret": True, "permissions": "0600"}


def write_private_export(path, data):
    path = Path(path)
    descriptor = None
    directory = None
    created = False
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OperatorError("export_parent_must_be_owned_and_0700")
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(directory)
    except OperatorError:
        raise
    except FileExistsError:
        raise OperatorError("export_destination_already_exists") from None
    except OSError:
        if created and directory is not None:
            try:
                os.unlink(path.name, dir_fd=directory)
            except OSError:
                pass
        raise OperatorError("export_file_write_failed") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("list")
    for command in ("show", "suspend", "resume", "revoke"):
        commands.add_parser(command).add_argument("name")
    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--expires", type=int, default=0, help="UTC Unix seconds; 0 means unlimited")
    create.add_argument("--limit-bytes", type=int, default=0, help="0 means unlimited")
    export = commands.add_parser("export")
    export.add_argument("name")
    export.add_argument("--output", type=Path, required=True, help="New file in an owned 0700 directory")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        operator = Operator(API(load_credentials(args.credentials)))
        if args.command == "status":
            answer = operator.status()
        elif args.command == "list":
            answer = operator.listing()
        elif args.command == "show":
            answer = operator.show(args.name)
        elif args.command == "create":
            answer = operator.create(args.name, args.expires, args.limit_bytes)
        elif args.command in ("suspend", "resume"):
            answer = operator.set_status(args.name, "disabled" if args.command == "suspend" else "active")
        elif args.command == "revoke":
            answer = operator.revoke(args.name)
        else:
            answer = operator.export(args.name, args.output)
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except OperatorError as error:
        print(json.dumps(error.as_dict()), file=sys.stderr)
        return 2
    except (KeyError, ValueError, TypeError, AttributeError, OSError):
        print(json.dumps({"error": "unexpected_operator_failure"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
