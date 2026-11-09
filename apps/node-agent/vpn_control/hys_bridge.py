"""Root-only, bounded Unix RPC bridge for API-owned legacy Hysteria users.

The public API runs unprivileged. Only this process reads the existing node's
Hysteria configuration and MongoDB, and no command/password is accepted over RPC.
"""
import argparse
from dataclasses import asdict
import datetime as dt
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import socket
import socketserver
import stat
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
import urllib.request
import urllib.error

from .common import BackendAccess, BackendConflict, BackendError, BackendNotFound, BackendUnavailable
from .config import private_json
from .hysteria import MAX_MESSAGE, USERNAME


def load_mongo_configuration(credentials_file=None):
    """Load a loopback-only Mongo connection without exposing its credentials.

    Existing NL installations retain the original unauthenticated local defaults.
    A fresh node can supply an authenticated URI through a private credential file.
    """
    if credentials_file is None:
        return {"uri": "mongodb://127.0.0.1:27017/", "database": "blitz_panel", "collection": "users"}
    try:
        value = private_json(Path(credentials_file))
        if set(value) != {"uri", "database", "collection"}:
            raise ValueError
        for key in ("database", "collection"):
            if not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,62}", value[key]):
                raise ValueError
        uri = value["uri"]
        if (not isinstance(uri, str) or len(uri) > 8192
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri)
                or re.search(r"%(?![0-9a-fA-F]{2})", uri)):
            raise ValueError
        location = urlsplit(uri)
        if (location.scheme != "mongodb" or location.hostname not in {"127.0.0.1", "::1", "localhost"}
                or location.port is None or not 1 <= location.port <= 65535
                or "," in location.netloc or location.fragment
                or location.path not in {"", "/", "/" + value["database"]}
                or (location.username is None) != (location.password is None)
                or location.username == "" or location.password == ""):
            raise ValueError
        pairs = parse_qsl(location.query, keep_blank_values=True, strict_parsing=True)
        options = dict(pairs)
        if (len(options) != len(pairs) or set(options) - {"authSource", "authMechanism"}
                or options.get("authSource", value["database"]) not in {"admin", value["database"]}
                or options.get("authMechanism", "SCRAM-SHA-256") != "SCRAM-SHA-256"):
            raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise BackendUnavailable("hysteria_mongo_credentials_invalid") from None


def connect_mongo(credentials_file=None, *, client_factory=None):
    """Select the configured collection; all driver/setup failures stay secret-free."""
    configuration = load_mongo_configuration(credentials_file)
    client = None
    try:
        if client_factory is None:
            import pymongo
            client_factory = pymongo.MongoClient
        client = client_factory(configuration["uri"], serverSelectionTimeoutMS=3000,
                                connectTimeoutMS=3000, socketTimeoutMS=3000)
        return client, client[configuration["database"]][configuration["collection"]]
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        raise BackendUnavailable("hysteria_mongo_connection_unavailable") from None


def validate_username(username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise BackendConflict("backend_username_not_owned")
    return username


def marker(username):
    return "vpn-control:" + username


def policy(expires_at, traffic_limit_bytes, activation_days, max_sessions=None):
    from .hysteria import HysteriaBackend
    return HysteriaBackend._policy(expires_at, traffic_limit_bytes, activation_days, max_sessions)


def session_limit(row):
    value = row.get("max_sessions")
    # BSON Int64 is an int subclass. Reject bool/coercion, preserve old rows.
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 128):
        raise BackendUnavailable("hysteria_invalid_session_policy")
    return int(value) if value is not None else None


def snapshot(row, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    used = max(0, int(row.get("download_bytes") or 0)) + max(0, int(row.get("upload_bytes") or 0))
    limit = max(0, int(row.get("max_download_bytes") or 0))
    days = int(row.get("expiration_days") or 0)
    expiry = None
    created = row.get("account_creation_date")
    if days > 0 and created:
        try:
            start = dt.datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            expiry = int((start + dt.timedelta(days=days)).timestamp())
        except (TypeError, ValueError, OverflowError):
            raise BackendUnavailable("hysteria_invalid_expiry_state") from None
    status = "active"
    if row.get("blocked") or row.get("_vpn_session_close_pending") is True:
        status = "suspended"
    elif expiry is not None and int(now.timestamp()) >= expiry:
        status = "expired"
    elif limit > 0 and used >= limit:
        status = "limited"
    return BackendAccess(status=status, used_traffic_bytes=used, traffic_limit_bytes=limit,
                         expires_at=expiry, expiry_precision="day", activation_days=days or None,
                         max_sessions=session_limit(row),
                         accounting="delayed_total_upload_download_counters")


def _local_http(url):
    parsed = urlsplit(url)
    try:
        local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        local = False
    if parsed.scheme != "http" or not local or parsed.username or parsed.password:
        raise BackendUnavailable("hysteria_local_endpoint_invalid")
    return url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class LegacyHysteria:
    """Exact legacy schema; metadata and secret export are separate operations."""
    def __init__(self, collection, server_config, *, public_host, public_port=1935,
                 tls_sni="github.com", profile_label="Hysteria", certificate_file=None, opener=None):
        self.collection = collection
        self.config_path = Path(server_config)
        self.public_host, self.public_port, self.tls_sni = public_host, public_port, tls_sni
        self.profile_label = profile_label
        self.certificate_file = str(certificate_file) if certificate_file is not None else None
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.lock = threading.RLock()

    def _config(self):
        try:
            return json.loads(self.config_path.read_bytes())
        except (OSError, ValueError):
            raise BackendUnavailable("hysteria_config_unavailable") from None

    def _row(self, username, required=False):
        validate_username(username)
        row = self.collection.find_one({"_id": username})
        if row is not None and row.get("note") != marker(username):
            raise BackendConflict("backend_username_not_owned")
        if row is None and required:
            raise BackendNotFound("backend_access_not_found")
        return row

    def _request(self, url, *, payload=None, secret=None, allow_missing=False):
        headers = {"Content-Type": "application/json"}
        if secret is not None:
            headers["Authorization"] = secret
        request = urllib.request.Request(_local_http(url),
                    data=json.dumps(payload).encode() if payload is not None else None,
                    headers=headers, method="POST" if payload is not None else "GET")
        try:
            with self.opener.open(request, timeout=3) as response:
                body = response.read(MAX_MESSAGE + 1)
                if len(body) > MAX_MESSAGE:
                    raise ValueError("oversized_backend_response")
                return json.loads(body) if body else None
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_missing:
                return None
            if error.code == 401 and secret is None:
                return {"ok": False}
            raise BackendUnavailable("hysteria_internal_endpoint_unavailable") from None
        except (OSError, ValueError):
            raise BackendUnavailable("hysteria_internal_endpoint_unavailable") from None

    def _session_capabilities(self):
        """Require both lifecycle enforcement and the active HTTP policy source.

        The engine alone cannot impose a Mongo field ignored by an older auth
        callback. These read-only probes never authenticate a real account.
        """
        try:
            config = self._config()
            auth_type = config["auth"]["type"]
            if not isinstance(auth_type, str) or auth_type.lower() not in {"http", "https"}:
                return {"max_sessions": False}
            stats = config["trafficStats"]
            engine = self._request("http://" + stats["listen"] + "/capabilities",
                                   secret=stats["secret"], allow_missing=True)
            auth_url = urlsplit(config["auth"]["http"]["url"])
            policy_url = auth_url._replace(path="/capabilities", query="", fragment="").geturl()
            auth = self._request(policy_url, allow_missing=True)
        except (KeyError, TypeError, ValueError, BackendUnavailable):
            return {"max_sessions": False}
        supported = (isinstance(engine, dict) and engine.get("max_sessions") is True
                     and type(engine.get("version")) is int and engine["version"] == 1
                     and engine.get("session_scope") == "auth_id_per_server_instance"
                     and engine.get("enforcement") == "atomic_quic_connection"
                     and isinstance(auth, dict) and auth.get("max_sessions") is True
                     and type(auth.get("version")) is int and auth["version"] == 1
                     and auth.get("policy_source") == "mongo_max_sessions")
        return ({"max_sessions": True, "session_scope": engine["session_scope"],
                 "enforcement": engine["enforcement"], "version": 1,
                 "close_by_auth_id": engine.get("close_by_auth_id") is True}
                if supported else {"max_sessions": False})

    def _require_session_capability(self, max_sessions, *, close_required=False):
        capabilities = self._session_capabilities() if max_sessions is not None or close_required else {}
        if max_sessions is not None and not capabilities.get("max_sessions"):
            raise BackendUnavailable("hysteria_max_sessions_unsupported")
        if close_required and capabilities.get("close_by_auth_id") is not True:
            raise BackendUnavailable("hysteria_close_by_auth_id_unsupported")

    def _close_connections(self, username, *, required=False):
        """New engine close is immediate and has no legacy sticky-kick flag."""
        try:
            config = self._config()
            stats = config["trafficStats"]
            url = "http://" + stats["listen"]
            capabilities = self._request(url + "/capabilities", secret=stats["secret"], allow_missing=True)
            supported = (isinstance(capabilities, dict) and capabilities.get("close_by_auth_id") is True
                         and type(capabilities.get("version")) is int and capabilities["version"] == 1)
            if not supported:
                if required:
                    raise BackendUnavailable("hysteria_close_by_auth_id_unsupported")
                return False
            result = self._request(url + "/close", secret=stats["secret"], payload=[username])
            if not isinstance(result, dict) or type(result.get("closed")) is not int or result["closed"] < 0:
                raise BackendUnavailable("hysteria_close_unconfirmed")
            return True
        except (KeyError, TypeError, ValueError):
            raise BackendUnavailable("hysteria_stats_configuration_invalid") from None

    def _auth(self, row, allowed):
        config = self._config()
        try:
            url = config["auth"]["http"]["url"]
            password = row["password"]
        except (KeyError, TypeError):
            raise BackendUnavailable("hysteria_auth_configuration_invalid") from None
        deadline = time.monotonic() + 6
        while True:
            value = self._request(url, payload={"auth": row["_id"] + ":" + password,
                                               "addr": "127.0.0.1:1", "tx": 0})
            if not isinstance(value, dict) or type(value.get("ok")) is not bool:
                raise BackendUnavailable("hysteria_auth_response_invalid")
            if value["ok"] == allowed and (not allowed or value.get("id") == row["_id"]):
                return
            if time.monotonic() >= deadline:
                raise BackendUnavailable("hysteria_auth_state_unconfirmed")
            time.sleep(.15)

    def _kick(self, username):
        config = self._config()
        try:
            stats = config["trafficStats"]
            url = "http://" + stats["listen"]
            secret = stats["secret"]
        except (KeyError, TypeError):
            raise BackendUnavailable("hysteria_stats_configuration_invalid") from None
        self._request(url + "/kick", payload=[username], secret=secret)

    def _online_count(self, username):
        """Only positive evidence permits a best-effort permanent-revoke kick."""
        try:
            config = self._config()
            stats = config["trafficStats"]
            online = self._request("http://" + stats["listen"] + "/online", secret=stats["secret"])
        except (KeyError, TypeError, BackendUnavailable):
            return None
        value = online.get(username, 0) if isinstance(online, dict) else None
        return value if type(value) is int and value >= 0 else None

    def create(self, username, *, expires_at=None, traffic_limit_bytes=0, activation_days=None, max_sessions=None):
        validate_username(username)
        policy(expires_at, traffic_limit_bytes, activation_days, max_sessions)
        with self.lock:
            self._require_session_capability(max_sessions)
            row = self._row(username)
            if row is not None:
                if (row.get("max_download_bytes", 0), row.get("expiration_days", 0), session_limit(row)) != (traffic_limit_bytes, activation_days or 0, max_sessions):
                    raise BackendConflict("backend_access_policy_conflict")
            else:
                # Match canonical add_user.py -> db.database.add_user exactly:
                # counters and activation date are absent until legacy accounting
                # initializes them. Never pre-activate an account here.
                row = {"_id": username, "password": secrets.token_urlsafe(32),
                       "max_download_bytes": traffic_limit_bytes, "expiration_days": activation_days or 0,
                       "max_sessions": max_sessions,
                       "blocked": False, "unlimited_user": False, "status": "On-hold", "note": marker(username)}
                self.collection.insert_one(dict(row))
            # A successful auth probe may activate a first-use account. Provisioning
            # must not consume that lifecycle event; actual client acceptance does.
            return asdict(snapshot(self._row(username, True)))

    def get(self, username):
        row = self._row(username)
        return asdict(snapshot(row)) if row is not None else None

    def update(self, username, *, expires_at=None, traffic_limit_bytes=0, activation_days=None, max_sessions=None):
        policy(expires_at, traffic_limit_bytes, activation_days, max_sessions)
        with self.lock:
            row = self._row(username, True)
            previous_limit = session_limit(row)
            tightening = max_sessions is not None and (previous_limit is None or max_sessions < previous_limit)
            close_pending = max_sessions is not None and (tightening or row.get("_vpn_session_close_pending") is True)
            self._require_session_capability(max_sessions, close_required=close_pending)
            self.collection.update_one({"_id": username, "note": marker(username)}, {"$set": {
                "max_download_bytes": traffic_limit_bytes, "expiration_days": activation_days or 0,
                "max_sessions": max_sessions, "_vpn_session_close_pending": close_pending}})
            if close_pending:
                self._close_connections(username, required=True)
                self.collection.update_one({"_id": username, "note": marker(username)},
                                           {"$set": {"_vpn_session_close_pending": False}})
            row = self._row(username, True)
            current = snapshot(row)
            if current.status != "active":
                self._auth(row, False)
            # /kick persists until next traffic. Reversible updates must not
            # poison a future connection after the policy is extended again.
            return asdict(snapshot(self._row(username, True)))

    def set_status(self, username, status):
        if status not in {"active", "suspended"}:
            raise BackendConflict("invalid_access_status")
        with self.lock:
            row = self._row(username, True)
            candidate = {**row, "blocked": False}
            if status == "active":
                self._require_session_capability(session_limit(row))
                if row.get("_vpn_session_close_pending") is True:
                    raise BackendUnavailable("hysteria_session_close_pending")
            if status == "active" and snapshot(candidate).status != "active":
                raise BackendConflict("access_policy_inactive")
            self.collection.update_one({"_id": username, "note": marker(username)},
                                       {"$set": {"blocked": status == "suspended"}})
            row = self._row(username, True)
            if status == "suspended":
                self._auth(row, False)
                self._close_connections(username)
            # Patched engines close current QUIC connections without a sticky
            # kick. Legacy engines keep their new-authentication-only behavior.
            return asdict(snapshot(self._row(username, True)))

    def revoke(self, username):
        with self.lock:
            row = self._row(username)
            if row is not None:
                self.collection.update_one({"_id": username, "note": marker(username)}, {"$set": {"blocked": True}})
                self._auth(self._row(username, True), False)
            # Only permanent usernames may receive this best-effort operation.
            # Hysteria's /kick is a next-traffic flag, not an immediate close.
            closed = self._close_connections(username)
            if not closed and (self._online_count(username) or 0) > 0:
                try:
                    self._kick(username)
                except BackendUnavailable:
                    pass
            self.collection.delete_one({"_id": username, "note": marker(username)})
            if self._row(username) is not None:
                raise BackendUnavailable("hysteria_delete_unconfirmed")
            return None

    def profile(self, username):
        with self.lock:
            row = self._row(username, True)
            self._require_session_capability(session_limit(row))
            if row.get("_vpn_session_close_pending") is True:
                raise BackendUnavailable("hysteria_session_close_pending")
            if snapshot(row).status != "active":
                raise BackendConflict("profile_requires_active_access")
            config = self._config()
            try:
                # The engine may own a 0640 certificate under a separate UID.
                # A root bridge with no DAC capabilities cannot read it merely
                # because its UID is zero. Prefer an explicitly delivered PUBLIC
                # certificate (for example systemd LoadCredential); never grant
                # access to the engine's private key to make profile export work.
                cert = self.certificate_file or config["tls"]["cert"]
                obfs = config["obfs"]
                if obfs["type"] != "salamander":
                    raise ValueError("unsupported_obfuscation")
                password = obfs["salamander"]["password"]
                process = subprocess.run(["openssl", "x509", "-in", cert, "-outform", "DER"],
                                         capture_output=True, timeout=3, check=True)
                fingerprint = hashlib.sha256(process.stdout).hexdigest()
            except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
                raise BackendUnavailable("hysteria_profile_configuration_invalid") from None
            query = urlencode({"obfs": "salamander", "obfs-password": password,
                               "sni": self.tls_sni, "insecure": "1", "pinSHA256": fingerprint})
            auth = quote(username + ":" + row["password"], safe="")
            host = self.public_host
            if ":" in host and not host.startswith("["):
                host = "[" + host + "]"
            return "hysteria2://" + auth + "@" + host + ":" + str(self.public_port) + "/?" + query + "#" + quote(self.profile_label, safe="")

    def health(self):
        self.collection.database.command("ping")
        config = self._config()
        try:
            stats = config["trafficStats"]
            self._request("http://" + stats["listen"] + "/online", secret=stats["secret"])
            auth_url = config["auth"]["http"]["url"]
        except (KeyError, TypeError):
            raise BackendUnavailable("hysteria_stats_configuration_invalid") from None
        capabilities = self._session_capabilities()
        # Readiness must verify the auth listener without authenticating or
        # activating any real account. This identity is never inserted in Mongo.
        for _ in range(3):
            name = "vc_" + secrets.token_hex(12)
            if self.collection.find_one({"_id": name}) is None:
                break
        else:
            raise BackendUnavailable("hysteria_auth_health_unavailable")
        value = self._request(auth_url, payload={"auth": name + ":" + secrets.token_urlsafe(32),
                                                "addr": "127.0.0.1:1", "tx": 0})
        if not isinstance(value, dict) or value.get("ok") is not False:
            raise BackendUnavailable("hysteria_auth_health_unavailable")
        return {"ready": True, "expiry_precision": "day", "unlimited_traffic_supported": True,
                "unlimited_expiry_supported": True,
                "existing_stream_termination": "close_by_auth_id" if capabilities.get("close_by_auth_id") else "not_guaranteed",
                "suspend_enforcement": "new_auth_and_quic_close" if capabilities.get("close_by_auth_id") else "new_connections_only",
                "revoke_existing_sessions": "close_by_auth_id" if capabilities.get("close_by_auth_id") else "best_effort_kick_when_positively_online",
                "accounting": "delayed_total_upload_download_counters", "auth_ready": True,
                "capabilities": capabilities}


OPERATIONS = {
    "health": set(), "get": {"username"}, "profile": {"username"}, "revoke": {"username"},
    "set_status": {"username", "status"},
    "create": {"username", "expires_at", "traffic_limit_bytes", "activation_days"},
    "update": {"username", "expires_at", "traffic_limit_bytes", "activation_days"},
}


def dispatch(backend, request):
    if not isinstance(request, dict) or type(request.get("version")) is not int or request.get("version") != 1:
        raise BackendConflict("invalid_bridge_request")
    op = request.get("op")
    supplied = set(request) - {"version", "op"}
    optional = {"max_sessions"} if isinstance(op, str) and op in {"create", "update"} else set()
    if not isinstance(op, str) or op not in OPERATIONS or supplied - optional != OPERATIONS[op]:
        raise BackendConflict("invalid_bridge_request")
    kwargs = {k: v for k, v in request.items() if k not in {"version", "op"}}
    if "username" in kwargs:
        validate_username(kwargs["username"])
    return getattr(backend, op)(**kwargs)


class BridgeHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            _, uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in self.server.allowed_uids:
                return
            self.request.settimeout(10)
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                raise BackendConflict("invalid_bridge_request")
            result = dispatch(self.server.backend, json.loads(line))
            response = {"ok": True, "result": result}
        except BackendError as error:
            kind = "conflict" if isinstance(error, BackendConflict) else "not_found" if isinstance(error, BackendNotFound) else "unavailable"
            response = {"ok": False, "error": {"kind": kind, "code": error.code}}
        except Exception:
            response = {"ok": False, "error": {"kind": "unavailable", "code": "hysteria_bridge_unavailable"}}
        try:
            self.wfile.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        except OSError:
            pass


class BridgeServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self.worker_slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_slots.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/vpn-control-hys/bridge.sock")
    parser.add_argument("--group", default="vpn-control")
    parser.add_argument("--allowed-user", default="vpn-control")
    parser.add_argument("--server-config", default="/etc/hysteria/config.json")
    parser.add_argument("--public-host", default="195.133.195.40")
    parser.add_argument("--public-port", type=int, default=1935)
    parser.add_argument("--tls-sni", default="github.com")
    parser.add_argument("--profile-label", default="Hysteria")
    parser.add_argument("--certificate-file", help="Readable public TLS certificate, optionally delivered with LoadCredential")
    parser.add_argument("--mongo-credentials-file", help="Private JSON connection settings for an authenticated local MongoDB")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("bridge_requires_root")
    uid, gid = pwd.getpwnam(args.allowed_user).pw_uid, grp.getgrnam(args.group).gr_gid
    path = Path(args.socket)
    parent = path.parent.stat()
    if parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) & 0o022:
        raise SystemExit("unsafe_socket_directory")
    if path.exists():
        old = path.lstat()
        if not stat.S_ISSOCK(old.st_mode) or old.st_uid != 0:
            raise SystemExit("unsafe_socket_path")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
            existing.settimeout(.3)
            try:
                existing.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            except OSError:
                raise SystemExit("bridge_socket_status_unknown") from None
            else:
                raise SystemExit("bridge_socket_already_active")
        path.unlink()
    try:
        client, collection = connect_mongo(args.mongo_credentials_file)
    except BackendUnavailable as error:
        raise SystemExit(error.code) from None
    backend = LegacyHysteria(collection, args.server_config,
                             public_host=args.public_host, public_port=args.public_port, tls_sni=args.tls_sni,
                             profile_label=args.profile_label, certificate_file=args.certificate_file)
    try:
        with BridgeServer(str(path), BridgeHandler) as server:
            os.chown(path, 0, gid)
            path.chmod(0o660)
            server.allowed_uids = {0, uid}
            server.backend = backend
            server.serve_forever(poll_interval=.25)
    finally:
        client.close()
        if path.is_socket():
            path.unlink()


if __name__ == "__main__":
    main()
