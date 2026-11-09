"""Mongo-compatible Hysteria authorization, accounting and session enforcement.

Counters are never cleared. Policy revocation fences new authorization in Mongo
before requesting the patched engine's synchronous connection-close operation.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BODY = 16384
MAX_STATS = 4 * 1024 * 1024
META = "_vpn_collector"
POLICY_PENDING = "_vpn_policy_close_pending"
POLICY_CLOSED = "_vpn_policy_closed"
CAPABILITIES = {"max_sessions": True, "version": 1, "policy_source": "mongo_max_sessions",
                "quota_expiry_enforcement": True, "policy_close_fence": True,
                "enforcement_version": 1}
POLICY_FIELDS = ("blocked", "unlimited_user", "max_sessions", "max_download_bytes",
                 "expiration_days", "account_creation_date", "upload_bytes", "download_bytes")


class RuntimeStateError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def nonnegative_int(value):
    # PyMongo represents BSON int64 as an int subclass. Quotas above 2 GiB
    # must retain their valid integer semantics; bool remains invalid.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeStateError("invalid_counter")
    return int(value)


def policy_denial(row, now=None):
    """Return a finite denial code without mutating legacy account policy."""
    if row.get("blocked"):
        return "blocked"
    sessions = row.get("max_sessions")
    if sessions is not None and (not isinstance(sessions, int) or isinstance(sessions, bool)
                                 or not 1 <= sessions <= 128):
        return "invalid_policy"
    if row.get("unlimited_user") is True:
        return None
    try:
        limit = nonnegative_int(row.get("max_download_bytes", 0))
        days = nonnegative_int(row.get("expiration_days", 0))
        upload = nonnegative_int(row.get("upload_bytes", 0))
        download = nonnegative_int(row.get("download_bytes", 0))
        if limit and upload + download >= limit:
            return "quota"
        created = row.get("account_creation_date")
        # Legacy auth treats absent/null/empty dates as first-use hold. Do not
        # convert a retained legacy account into an invalid or expired policy.
        if days and created is not None and created != "":
            if not isinstance(created, str):
                return "invalid_policy"
            start = dt.datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            if start.strftime("%Y-%m-%d") != created:
                return "invalid_policy"
            current = now or dt.datetime.now(dt.timezone.utc)
            if current.tzinfo is None:
                return "invalid_policy"
            if current >= start + dt.timedelta(days=days):
                return "expired"
    except (RuntimeStateError, ValueError, TypeError, OverflowError):
        return "invalid_policy"
    return None


def authorize(collection, auth, now=None):
    """Read once per request; never create dates, accounts or usage counters."""
    if not isinstance(auth, str) or ":" not in auth:
        return {"ok": False}
    try:
        if len(auth.encode("utf-8")) > MAX_BODY:
            return {"ok": False}
    except UnicodeError:
        return {"ok": False}
    username, password = auth.split(":", 1)
    if not username or len(username) > 256:
        return {"ok": False}
    try:
        row = collection.find_one({"_id": username})
    except Exception:
        raise RuntimeStateError("mongo_unavailable") from None
    if (row is None or row.get("blocked") or row.get("_vpn_session_close_pending") is True
            or POLICY_PENDING in row):
        return {"ok": False}
    secret = row.get("password")
    if not isinstance(secret, str):
        return {"ok": False}
    try:
        if not hmac.compare_digest(secret.encode(), password.encode()):
            return {"ok": False}
    except UnicodeError:
        return {"ok": False}
    # This is an authentication policy only. The patched server reserves and
    # releases the slot atomically for the whole QUIC connection, not streams/IPs.
    sessions = row.get("max_sessions")
    if sessions is not None and (not isinstance(sessions, int) or isinstance(sessions, bool) or not 1 <= sessions <= 128):
        return {"ok": False}
    accepted = {"ok": True, "id": username}
    if sessions is not None:
        accepted["max_sessions"] = int(sessions)
    if policy_denial(row, now=now) is not None:
        return {"ok": False}
    return accepted


def _policy_fingerprint(row, reason):
    value = {field: {"present": field in row, "value": row.get(field)} for field in POLICY_FIELDS}
    value["reason"] = reason
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError):
        # Malformed legacy fields still deny and close; avoid exposing their
        # values or requiring an unserializable policy to become auth-valid.
        raw = repr(tuple((field, type(row.get(field)).__name__) for field in POLICY_FIELDS))
    return hashlib.sha256(raw.encode()).hexdigest()


def _policy_query(row):
    query = {"_id": row["_id"]}
    for field in (*POLICY_FIELDS, POLICY_PENDING, POLICY_CLOSED):
        query[field] = ({"$exists": True, "$eq": row[field]} if field in row
                        else {"$exists": False})
    state = row.get(META)
    query[META + ".seq"] = ({"$exists": True, "$eq": state["seq"]}
                               if isinstance(state, dict) and "seq" in state
                               else {"$exists": False})
    return query


def enforce_policies(collection, epoch, close_sessions, now=None):
    """Persist denial fences, close live connections, then acknowledge by token.

    Policy, quota and accounting fields are never rewritten. An ambiguous write
    or HTTP result retains the durable fence for the next pass; `/close` is safe
    to repeat. Acknowledgement suppresses only replay of the same snapshot.
    New snapshots retry closure even for still-expired idle users, avoiding an
    old acknowledgement hiding newly admitted sessions after a policy change.
    """
    if not isinstance(epoch, str) or not epoch or len(epoch) > 256:
        raise RuntimeStateError("collector_epoch_invalid")
    try:
        names = [row["_id"] for row in collection.find({}, {"_id": 1})]
    except Exception:
        raise RuntimeStateError("mongo_unavailable") from None
    observed = closed = pending = 0
    failed = False
    for username in names:
        observed += 1
        try:
            for _ in range(4):
                row = collection.find_one({"_id": username})
                if row is None:
                    break
                reason = policy_denial(row, now=now)
                marker = row.get(POLICY_PENDING)
                if POLICY_PENDING not in row and reason is None:
                    if POLICY_CLOSED in row:
                        result = collection.update_one(_policy_query(row), {"$unset": {POLICY_CLOSED: ""}})
                        if not result.matched_count:
                            continue
                    break
                state = row.get(META)
                seq = nonnegative_int(state.get("seq", 0)) if isinstance(state, dict) else 0
                fingerprint = _policy_fingerprint(row, reason)
                acknowledgement = {"epoch": epoch, "policy": fingerprint, "seq": seq}
                if POLICY_PENDING not in row and row.get(POLICY_CLOSED) == acknowledgement:
                    break
                if (not isinstance(marker, dict) or not isinstance(marker.get("token"), str)
                        or not marker["token"] or len(marker["token"]) > 128):
                    marker = {"token": uuid.uuid4().hex, "policy": fingerprint,
                              "reason": reason or "pending"}
                    result = collection.update_one(_policy_query(row), {"$set": {POLICY_PENDING: marker}})
                    if not result.matched_count:
                        continue
                # The durable marker denies auth even if an administrator now
                # extends the quota. Existing sessions close before readmission.
                response = close_sessions([username])
                if (not isinstance(response, dict) or type(response.get("closed")) is not int
                        or response["closed"] < 0):
                    raise RuntimeStateError("policy_close_unconfirmed")
                closed += response["closed"]
                result = collection.update_one({"_id": username, POLICY_PENDING + ".token": marker["token"]},
                    {"$unset": {POLICY_PENDING: ""}, "$set": {POLICY_CLOSED: acknowledgement}})
                if not result.matched_count:
                    pending += 1
                    failed = True
                break
            else:
                pending += 1
                failed = True
        except Exception:
            # Try every other user; one failed close must not starve unrelated
            # expired accounts. Public logs contain only finite error codes.
            pending += 1
            failed = True
    if failed:
        raise RuntimeStateError("policy_close_unconfirmed")
    return {"observed": observed, "closed": closed, "pending": pending}


def apply_snapshot(collection, epoch, traffic, online, today=None):
    """Persist each user's delta and cursor together with Mongo compare-and-set.

    A successful-but-unacknowledged write is safe to replay: the next read sees
    its cursor. Other writers' quota/status policy and counters are preserved.
    """
    if not isinstance(epoch, str) or not epoch or len(epoch) > 256:
        raise RuntimeStateError("collector_epoch_invalid")
    if not isinstance(traffic, dict) or not isinstance(online, dict):
        raise RuntimeStateError("collector_snapshot_invalid")
    day = (today or dt.datetime.now(dt.timezone.utc).date()).isoformat()
    observed = updated = 0
    # Include existing idle users to update online/status without activating them.
    try:
        names = [row["_id"] for row in collection.find({}, {"_id": 1})]
    except Exception:
        raise RuntimeStateError("mongo_unavailable") from None
    for username in names:
        values = traffic.get(username, {})
        if not isinstance(values, dict):
            raise RuntimeStateError("collector_snapshot_invalid")
        tx = nonnegative_int(values.get("tx", 0))
        rx = nonnegative_int(values.get("rx", 0))
        count = nonnegative_int(online.get(username, 0))
        observed += 1
        for _ in range(4):
            try:
                row = collection.find_one({"_id": username})
            except Exception:
                raise RuntimeStateError("mongo_unavailable") from None
            if row is None:
                break
            state = row.get(META)
            if META in row and not isinstance(state, dict):
                raise RuntimeStateError("collector_cursor_invalid")
            state = state or {}
            seq = nonnegative_int(state.get("seq", 0))
            previous_tx = nonnegative_int(state.get("tx", 0))
            previous_rx = nonnegative_int(state.get("rx", 0))
            same_epoch = state.get("epoch") == epoch
            # Hysteria omits empty records. Preserve a known cursor for an absent
            # record, rather than interpreting omission as a destructive reset.
            current_tx = tx if username in traffic or not same_epoch else previous_tx
            current_rx = rx if username in traffic or not same_epoch else previous_rx
            if same_epoch and (current_tx < previous_tx or current_rx < previous_rx):
                raise RuntimeStateError("collector_counter_regressed")
            delta_tx = current_tx - previous_tx if same_epoch else current_tx
            delta_rx = current_rx - previous_rx if same_epoch else current_rx
            created = row.get("account_creation_date")
            activated = created is not None and created != ""
            active = count > 0 or delta_tx > 0 or delta_rx > 0
            status = "Online" if count else "Offline" if activated or active else "On-hold"
            updates = {META: {"epoch": epoch, "tx": current_tx, "rx": current_rx, "seq": seq + 1},
                       "online_count": count, "status": status}
            if not activated and active:
                updates["account_creation_date"] = day
            query = {"_id": username, META + ".seq": seq if "seq" in state else {"$exists": False}}
            if "account_creation_date" in updates:
                # Do not overwrite an activation set by another collector or
                # administrator between this read and the atomic delta write.
                query["account_creation_date"] = (
                    {"$exists": True, "$eq": created}
                    if "account_creation_date" in row else {"$exists": False})
            try:
                result = collection.update_one(query, {"$inc": {"upload_bytes": delta_tx, "download_bytes": delta_rx},
                                                       "$set": updates})
            except Exception:
                raise RuntimeStateError("mongo_update_unconfirmed") from None
            if result.matched_count:
                updated += 1
                break
        else:
            raise RuntimeStateError("collector_concurrent_update")
    return {"observed": observed, "updated": updated}


def load_config(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid())
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)):
            raise RuntimeStateError("config_must_be_private")
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise RuntimeStateError("config_too_large")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeStateError("config_invalid") from None
    return value


def mongo(config):
    from pymongo import MongoClient
    from pymongo.write_concern import WriteConcern
    client = MongoClient(config.get("mongo_uri", "mongodb://127.0.0.1:27017/"),
                         serverSelectionTimeoutMS=2000, connectTimeoutMS=2000, socketTimeoutMS=3000)
    database = client[config.get("database", "blitz_panel")]
    collection = database.get_collection(config.get("collection", "users"), write_concern=WriteConcern(w=1, j=True))
    return client, collection


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # The base implementation includes a traceback and request context.
        pass


def make_handler(collection, ping, engine_supported=None, close_supported=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_):
            pass

        def send_json(self, status, body):
            raw = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/capabilities":
                self.send_json(200, CAPABILITIES)
            elif self.path == "/healthz":
                self.send_json(200, {"status": "ok"})
            elif self.path == "/readyz":
                try:
                    ping()
                except Exception:
                    self.send_json(503, {"status": "unavailable"})
                else:
                    self.send_json(200, {"status": "ready"})
            elif self.path == "/auth":
                self.send_json(405, {"error": "method_not_allowed"})
            else:
                self.send_json(404, {"error": "not_found"})

        def do_POST(self):
            if self.path != "/auth":
                self.send_json(404, {"error": "not_found"})
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get("Transfer-Encoding") is not None or len(lengths) != 1:
                self.send_json(400, {"error": "invalid_body_length"})
                return
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.send_json(413 if length > MAX_BODY else 400, {"error": "invalid_body_length"})
                return
            try:
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError
            except (ValueError, socket.timeout):
                self.send_json(400, {"error": "invalid_body"})
                return
            try:
                response = authorize(collection, value.get("auth"))
            except RuntimeStateError:
                self.send_json(503, {"ok": False})
            else:
                if response.get("ok") is True and response.get("max_sessions") is not None:
                    try:
                        supported = engine_supported is not None and engine_supported() is True
                    except Exception:
                        supported = False
                    if not supported:
                        response = {"ok": False}
                if response.get("ok") is True:
                    try:
                        username = value["auth"].split(":", 1)[0]
                        row = collection.find_one({"_id": username})
                        if (row is None or POLICY_PENDING in row
                                or row.get("_vpn_session_close_pending") is True
                                or policy_denial(row) is not None):
                            response = {"ok": False}
                        elif row.get("unlimited_user") is not True and (
                            nonnegative_int(row.get("max_download_bytes", 0)) > 0
                            or nonnegative_int(row.get("expiration_days", 0)) > 0
                        ) and not (close_supported is not None and close_supported() is True):
                            response = {"ok": False}
                    except Exception:
                        response = {"ok": False}
                self.send_json(200, response)

    return Handler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch_stats(config, suffix, *, payload=None):
    base = config.get("stats_url", "http://127.0.0.1:25413").rstrip("/")
    parsed = urlsplit(base)
    try:
        allowed = ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError):
        allowed = parsed.hostname == "localhost"
    if (not allowed or parsed.scheme != "http" or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise RuntimeStateError("stats_endpoint_must_be_loopback")
    secret = config.get("stats_secret")
    if not isinstance(secret, str) or not secret or "\r" in secret or "\n" in secret:
        raise RuntimeStateError("stats_secret_invalid")
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Authorization": secret}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base + suffix, headers=headers, data=body)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(MAX_STATS + 1)
        if len(raw) > MAX_STATS:
            raise ValueError
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception:
        raise RuntimeStateError("stats_unavailable") from None


def engine_caps(config):
    """A saved session limit must never become unlimited after a downgrade."""
    try:
        value = fetch_stats(config, "/capabilities")
        return (value.get("max_sessions") is True
                and value.get("session_scope") == "auth_id_per_server_instance"
                and value.get("enforcement") == "atomic_quic_connection"
                and type(value.get("version")) is int and value["version"] == 1)
    except (RuntimeStateError, ValueError, TypeError, AttributeError):
        return False


def engine_close_caps(config):
    try:
        value = fetch_stats(config, "/capabilities")
        return (value.get("close_by_auth_id") is True
                and value.get("session_scope") == "auth_id_per_server_instance"
                and value.get("enforcement") == "atomic_quic_connection"
                and type(value.get("version")) is int and value["version"] == 1)
    except (RuntimeStateError, ValueError, TypeError, AttributeError):
        return False


def close_sessions(config, usernames):
    if (not isinstance(usernames, list) or not usernames
            or any(not isinstance(name, str) or not name or len(name) > 256 for name in usernames)):
        raise RuntimeStateError("policy_close_invalid_identity")
    if not engine_close_caps(config):
        raise RuntimeStateError("policy_close_unsupported")
    return fetch_stats(config, "/close", payload=usernames)


def collect_once(collection, config, *, accounting_only=False):
    epoch_path = Path(config.get("epoch_file", "/run/vpn-stack/hysteria-epoch"))
    try:
        epoch = epoch_path.read_text().strip()
        traffic = fetch_stats(config, "/traffic?clear=false")
        online = fetch_stats(config, "/online")
        if epoch_path.read_text().strip() != epoch:
            raise RuntimeStateError("collector_epoch_changed")
    except OSError:
        raise RuntimeStateError("collector_epoch_unavailable") from None
    try:
        result = apply_snapshot(collection, epoch, traffic, online)
    except RuntimeStateError:
        # An invalid cursor for one account must not keep another already
        # expired/blocked session alive. Preserve the original accounting error.
        if not accounting_only:
            try:
                enforce_policies(collection, epoch, lambda names: close_sessions(config, names))
            except RuntimeStateError:
                pass
        raise
    if not accounting_only:
        enforced = enforce_policies(collection, epoch, lambda names: close_sessions(config, names))
        result.update({"closed": enforced["closed"], "policy_pending": enforced["pending"]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("auth", "collector"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--accounting-only", action="store_true",
                        help="One collector pass for a quiesced legacy-engine accounting handoff; does not close sessions")
    args = parser.parse_args()
    if args.accounting_only and (args.mode != "collector" or not args.once):
        parser.error("--accounting-only requires collector --once")
    client = None
    try:
        config = load_config(args.config)
        client, collection = mongo(config)
        if args.mode == "auth":
            host = config.get("listen_host", "127.0.0.1")
            if not ipaddress.ip_address(host).is_loopback:
                raise RuntimeStateError("auth_must_bind_loopback")
            port = config.get("listen_port", 28262)
            if type(port) is not int or not 1024 <= port <= 65535:
                raise RuntimeStateError("listen_port_invalid")
            with BoundedServer((host, port), make_handler(collection, lambda: client.admin.command("ping"),
                                                       lambda: engine_caps(config),
                                                       lambda: engine_close_caps(config))) as server:
                server.serve_forever(poll_interval=.5)
        else:
            interval = config.get("interval_seconds", 60)
            if type(interval) is not int or not 1 <= interval <= 3600:
                raise RuntimeStateError("collector_interval_invalid")
            lock_path = config.get("lock_file", "/run/vpn-stack/collector.lock")
            with open(lock_path, "a", encoding="utf-8") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                while True:
                    try:
                        result = collect_once(collection, config, accounting_only=args.accounting_only)
                    except RuntimeStateError as error:
                        print(json.dumps({"status": "unavailable", "code": error.code}), flush=True)
                        if args.once:
                            raise SystemExit(1)
                    else:
                        print(json.dumps({"status": "collected", **result}), flush=True)
                    if args.once:
                        break
                    time.sleep(interval)
    except KeyboardInterrupt:
        pass
    except (RuntimeStateError, OSError, ValueError, TypeError):
        print(json.dumps({"status": "unavailable", "code": "runtime_start_failed"}), flush=True)
        raise SystemExit(1)
    finally:
        if client is not None:
            client.close()
