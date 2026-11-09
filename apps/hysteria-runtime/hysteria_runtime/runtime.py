"""Mongo-schema-compatible Hysteria auth/collector for new isolated nodes.

Production adoption does not switch an existing auth or traffic collector.
This implementation never calls the destructive traffic clear or kick APIs.
"""
import argparse
import datetime as dt
import fcntl
import hmac
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_BODY = 16384
MAX_STATS = 4 * 1024 * 1024
META = "_vpn_collector"


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
    if row is None or row.get("blocked"):
        return {"ok": False}
    secret = row.get("password")
    if not isinstance(secret, str):
        return {"ok": False}
    try:
        if not hmac.compare_digest(secret.encode(), password.encode()):
            return {"ok": False}
    except UnicodeError:
        return {"ok": False}
    if row.get("unlimited_user") is True:
        return {"ok": True, "id": username}
    try:
        limit = nonnegative_int(row.get("max_download_bytes", 0))
        days = nonnegative_int(row.get("expiration_days", 0))
        upload = nonnegative_int(row.get("upload_bytes", 0))
        download = nonnegative_int(row.get("download_bytes", 0))
        if limit and upload + download >= limit:
            return {"ok": False}
        created = row.get("account_creation_date")
        if days and created:
            if not isinstance(created, str):
                return {"ok": False}
            start = dt.datetime.strptime(created, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
            expiry = start + dt.timedelta(days=days)
            now = now or dt.datetime.now(dt.timezone.utc)
            if now.tzinfo is None or now >= expiry:
                return {"ok": False}
    except (RuntimeStateError, ValueError, TypeError, OverflowError):
        return {"ok": False}
    return {"ok": True, "id": username}


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
            activated = "account_creation_date" in row
            active = count > 0 or delta_tx > 0 or delta_rx > 0
            status = "Online" if count else "Offline" if activated or active else "On-hold"
            updates = {META: {"epoch": epoch, "tx": current_tx, "rx": current_rx, "seq": seq + 1},
                       "online_count": count, "status": status}
            if not activated and active:
                updates["account_creation_date"] = day
            query = {"_id": username, META + ".seq": seq if "seq" in state else {"$exists": False}}
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


def make_handler(collection, ping):
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
            if self.path == "/healthz":
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
                self.send_json(200, response)

    return Handler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def fetch_stats(config, suffix):
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
    request = Request(base + suffix, headers={"Authorization": secret})
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


def collect_once(collection, config):
    epoch_path = Path(config.get("epoch_file", "/run/vpn-stack/hysteria-epoch"))
    try:
        epoch = epoch_path.read_text().strip()
        traffic = fetch_stats(config, "/traffic?clear=false")
        online = fetch_stats(config, "/online")
        if epoch_path.read_text().strip() != epoch:
            raise RuntimeStateError("collector_epoch_changed")
    except OSError:
        raise RuntimeStateError("collector_epoch_unavailable") from None
    return apply_snapshot(collection, epoch, traffic, online)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("auth", "collector"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
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
            with BoundedServer((host, port), make_handler(collection, lambda: client.admin.command("ping"))) as server:
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
                        result = collect_once(collection, config)
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
