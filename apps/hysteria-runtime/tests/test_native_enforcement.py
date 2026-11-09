"""Real Mongo -> runtime HTTP -> native QUIC -> policy connection close.

Opt in with HYS_RUNTIME_TEST_MONGO and HYS_RUNTIME_TEST_ENGINE. Only a new
random local database, loopback sockets and private temporary files are used.
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

from hysteria_runtime.runtime import (
    BoundedServer, apply_snapshot, authorize, collect_once, engine_caps, engine_close_caps,
    fetch_stats, make_handler,
)
from serve_native_fixture import local_mongo_uri


def unused_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def receive(stream, count):
    raw = b""
    while len(raw) < count:
        part = stream.recv(count - len(raw))
        if not part:
            raise EOFError("fixture stream closed")
        raw += part
    return raw


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            while True:
                raw = self.request.recv(16384)
                if not raw:
                    return
                self.request.sendall(raw)
        except (OSError, TimeoutError):
            pass


class Origin(socketserver.ThreadingTCPServer):
    daemon_threads = True


class MongoFirstUseTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv("HYS_RUNTIME_TEST_MONGO"), "Set explicit local Mongo URI")
    def test_null_and_empty_first_use_are_atomic_under_concurrent_snapshot_replay(self):
        from pymongo import MongoClient
        from pymongo.write_concern import WriteConcern
        uri = local_mongo_uri(os.environ["HYS_RUNTIME_TEST_MONGO"])
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        database = "vpn_hys_first_use_" + uuid.uuid4().hex
        collection = client[database].get_collection("users", write_concern=WriteConcern(w=1, j=True))
        today = dt.date(2026, 11, 9)
        try:
            client.admin.command("ping")
            for initial_date in (None, ""):
                with self.subTest(initial_date=initial_date):
                    username = "first_use_" + uuid.uuid4().hex
                    collection.insert_one({"_id": username, "password": "public-first-use-fixture",
                        "expiration_days": 7, "account_creation_date": initial_date,
                        "upload_bytes": 100, "download_bytes": 200})
                    before = collection.find_one({"_id": username})
                    self.assertTrue(authorize(collection, username + ":public-first-use-fixture")["ok"])
                    self.assertEqual(collection.find_one({"_id": username}), before)
                    apply_snapshot(collection, "epoch-one", {}, {}, today=today)
                    self.assertEqual(collection.find_one({"_id": username})["account_creation_date"], initial_date)
                    barrier = threading.Barrier(2)
                    arrivals = threading.local()

                    class RacedCollection:
                        def find(self, *args, **kwargs):
                            return collection.find(*args, **kwargs)

                        def find_one(self, *args, **kwargs):
                            return collection.find_one(*args, **kwargs)

                        def update_one(self, query, update):
                            if (query["_id"] == username and "account_creation_date" in update.get("$set", {})
                                    and not getattr(arrivals, "seen", False)):
                                arrivals.seen = True
                                barrier.wait(timeout=5)
                            return collection.update_one(query, update)

                    snapshot = {username: {"tx": 11, "rx": 13}}
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(apply_snapshot, RacedCollection(), "epoch-one", snapshot, {}, today)
                                   for _ in range(2)]
                        for future in futures:
                            future.result(timeout=10)
                    row = collection.find_one({"_id": username})
                    self.assertEqual(row["account_creation_date"], "2026-11-09")
                    self.assertEqual((row["upload_bytes"], row["download_bytes"]), (111, 213))
                    apply_snapshot(collection, "epoch-one", snapshot, {}, today=today + dt.timedelta(days=1))
                    row = collection.find_one({"_id": username})
                    self.assertEqual(row["account_creation_date"], "2026-11-09")
                    self.assertEqual((row["upload_bytes"], row["download_bytes"]), (111, 213))
                    before_expiry = dt.datetime(2026, 11, 15, 23, 59, 59, tzinfo=dt.timezone.utc)
                    at_expiry = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)
                    self.assertTrue(authorize(collection, username + ":public-first-use-fixture", now=before_expiry)["ok"])
                    self.assertFalse(authorize(collection, username + ":public-first-use-fixture", now=at_expiry)["ok"])
        finally:
            client.drop_database(database)
            client.close()


class NativePolicyEnforcementTests(unittest.TestCase):
    @unittest.skipUnless(os.getenv("HYS_RUNTIME_TEST_MONGO") and os.getenv("HYS_RUNTIME_TEST_ENGINE"),
                         "Set explicit local Mongo URI and tested native engine path")
    def test_quota_and_expiry_close_existing_quic_stream_and_preserve_usage(self):
        from pymongo import MongoClient
        from pymongo.write_concern import WriteConcern
        from bson.int64 import Int64

        uri = local_mongo_uri(os.environ["HYS_RUNTIME_TEST_MONGO"])
        engine = Path(os.environ["HYS_RUNTIME_TEST_ENGINE"]).resolve(strict=True)
        self.assertTrue(engine.is_file() and os.access(engine, os.X_OK))
        client = MongoClient(uri, serverSelectionTimeoutMS=2000)
        database = "vpn_hys_policy_native_" + uuid.uuid4().hex
        collection = client[database].get_collection("users", write_concern=WriteConcern(w=1, j=True))
        processes = []
        servers = []
        streams = []
        logs = []
        try:
            client.admin.command("ping")
            with tempfile.TemporaryDirectory(prefix="vpn-hys-policy-native-") as directory:
                root = Path(directory)
                cert, key = root / "cert.pem", root / "key.pem"
                subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                                "-days", "1", "-subj", "/CN=localhost",
                                "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key),
                                "-out", str(cert)], check=True, capture_output=True, timeout=15)
                key.chmod(0o600)
                stats_port, udp_port = unused_port(), unused_port(socket.SOCK_DGRAM)
                config = {"stats_url": f"http://127.0.0.1:{stats_port}",
                          "stats_secret": "public-native-policy-fixture-secret",
                          "epoch_file": str(root / "epoch")}
                (root / "epoch").write_text("native-policy-" + uuid.uuid4().hex)
                auth = BoundedServer(("127.0.0.1", 0), make_handler(
                    collection, lambda: client.admin.command("ping"),
                    lambda: engine_caps(config), lambda: engine_close_caps(config)))
                auth_thread = threading.Thread(target=auth.serve_forever, daemon=True)
                auth_thread.start()
                servers.append((auth, auth_thread))
                origin = Origin(("127.0.0.1", 0), Echo)
                origin_thread = threading.Thread(target=origin.serve_forever, daemon=True)
                origin_thread.start()
                servers.append((origin, origin_thread))

                def launch(mode, value, name):
                    path = root / (name + ".json")
                    path.write_text(json.dumps(value))
                    path.chmod(0o600)
                    log = open(root / (name + ".log"), "wb")
                    logs.append(log)
                    process = subprocess.Popen([str(engine), mode, "-c", str(path),
                                                "--disable-update-check", "--log-level", "warn"],
                                               stdout=log, stderr=subprocess.STDOUT)
                    processes.append(process)
                    return process

                server = launch("server", {
                    "listen": f"127.0.0.1:{udp_port}",
                    "tls": {"cert": str(cert), "key": str(key)},
                    "auth": {"type": "http", "http": {"url": f"http://127.0.0.1:{auth.server_port}/auth"}},
                    "trafficStats": {"listen": f"127.0.0.1:{stats_port}", "secret": config["stats_secret"]},
                }, "server")
                for _ in range(100):
                    if server.poll() is not None:
                        self.fail("Native fixture engine exited before readiness")
                    if engine_caps(config) and engine_close_caps(config):
                        break
                    time.sleep(.05)
                else:
                    self.fail("Native fixture capabilities were unavailable")

                for scenario in ("quota", "expiry"):
                    with self.subTest(scenario=scenario):
                        username = "native_policy_" + scenario
                        collection.insert_one({"_id": username, "password": "public-fixture-password",
                            "blocked": False, "unlimited_user": False, "max_sessions": Int64(1),
                            "max_download_bytes": Int64(1024 * 1024), "expiration_days": 0,
                            "upload_bytes": Int64(31), "download_bytes": Int64(47)})
                        socks_port = unused_port()
                        native_client = launch("client", {
                            "server": f"127.0.0.1:{udp_port}",
                            "auth": username + ":public-fixture-password",
                            "tls": {"ca": str(cert), "sni": "localhost"},
                            "socks5": {"listen": f"127.0.0.1:{socks_port}"},
                        }, scenario)
                        for _ in range(100):
                            if native_client.poll() is not None:
                                self.fail("Native client exited before SOCKS readiness")
                            try:
                                stream = socket.create_connection(("127.0.0.1", socks_port), timeout=.1)
                                break
                            except OSError:
                                time.sleep(.05)
                        else:
                            self.fail("Native client SOCKS listener unavailable")
                        streams.append(stream)
                        stream.settimeout(5)
                        stream.sendall(b"\x05\x01\x00")
                        self.assertEqual(receive(stream, 2), b"\x05\x00")
                        stream.sendall(b"\x05\x01\x00\x01\x7f\x00\x00\x01" + struct.pack("!H", origin.server_address[1]))
                        reply = receive(stream, 4)
                        self.assertEqual(reply[:2], b"\x05\x00")
                        self.assertEqual(reply[3], 1)
                        receive(stream, 6)
                        payload = b"native-enforcement-fixture-" * 128
                        stream.sendall(payload)
                        self.assertEqual(receive(stream, len(payload)), payload)
                        for _ in range(50):
                            traffic = fetch_stats(config, "/traffic?clear=false")
                            if traffic.get(username, {}).get("tx", 0) + traffic.get(username, {}).get("rx", 0) > 0:
                                break
                            time.sleep(.05)
                        self.assertGreater(traffic[username]["tx"] + traffic[username]["rx"], 0)
                        if scenario == "quota":
                            collection.update_one({"_id": username}, {"$set": {"max_download_bytes": Int64(1)}})
                        else:
                            expired = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=2)).isoformat()
                            collection.update_one({"_id": username}, {"$set": {
                                "expiration_days": 1, "account_creation_date": expired}})
                        result = collect_once(collection, config)
                        self.assertGreaterEqual(result["closed"], 1)
                        row = collection.find_one({"_id": username})
                        self.assertFalse(row["blocked"])
                        self.assertFalse(row["unlimited_user"])
                        self.assertEqual(row["password"], "public-fixture-password")
                        self.assertGreaterEqual(row["upload_bytes"], 31)
                        self.assertGreaterEqual(row["download_bytes"], 47)
                        self.assertGreater(row["upload_bytes"] + row["download_bytes"], 78)
                        self.assertNotIn("_vpn_policy_close_pending", row)
                        self.assertEqual(authorize(collection, username + ":public-fixture-password"), {"ok": False})
                        # Closing real QUIC invalidates the already-open SOCKS stream.
                        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                            stream.sendall(b"must-not-echo")
                        try:
                            self.assertEqual(stream.recv(128), b"")
                        except ConnectionResetError:
                            pass
                        stream.close()
                        streams.remove(stream)
                        native_client.terminate()
                        native_client.wait(timeout=5)
        finally:
            for stream in streams:
                stream.close()
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            for server, thread in servers:
                server.shutdown()
                server.server_close()
                thread.join(3)
            for log in logs:
                log.close()
            client.drop_database(database)
            client.close()


if __name__ == "__main__":
    unittest.main()
