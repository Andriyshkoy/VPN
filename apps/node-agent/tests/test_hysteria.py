import copy
from dataclasses import asdict
import datetime as dt
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
import hashlib

from vpn_control.common import BackendConflict, BackendNotFound, BackendUnavailable
from vpn_control.hysteria import HysteriaBackend
from vpn_control.hys_bridge import BridgeHandler, BridgeServer, LegacyHysteria, dispatch, marker, snapshot


USER = "vc_" + "a" * 24
OTHER = "vc_" + "b" * 24


class Collection:
    def __init__(self):
        self.rows = {}

    def find_one(self, query):
        row = self.rows.get(query["_id"])
        if row is None or any(row.get(k) != v for k, v in query.items()):
            return None
        return copy.deepcopy(row)

    def insert_one(self, row):
        if row["_id"] in self.rows:
            raise RuntimeError("duplicate")
        self.rows[row["_id"]] = copy.deepcopy(row)

    def update_one(self, query, update):
        if self.find_one(query) is not None:
            self.rows[query["_id"]].update(copy.deepcopy(update["$set"]))

    def delete_one(self, query):
        if self.find_one(query) is not None:
            del self.rows[query["_id"]]


class Backend(LegacyHysteria):
    def __init__(self):
        super().__init__(Collection(), "/unused", public_host="example.invalid")
        self.auth_calls, self.kicks = [], []
        self.kick_unavailable = False
        self.online = {}
        self.pending_kicks = set()

    def _auth(self, row, allowed):
        self.auth_calls.append((row["_id"], allowed))
        if (snapshot(row).status == "active") != allowed:
            raise AssertionError("incorrect expected authentication state")

    def _kick(self, username):
        self.kicks.append(username)
        if self.kick_unavailable:
            raise BackendUnavailable("hysteria_internal_endpoint_unavailable")
        self.pending_kicks.add(username)

    def _online_count(self, username):
        return self.online.get(username, 0)

    def first_new_data_succeeds(self, username):
        if username in self.pending_kicks:
            self.pending_kicks.remove(username)
            return False
        return True


class PolicyTests(unittest.TestCase):
    def test_quota_counts_both_directions_and_zero_means_unlimited(self):
        row = {"download_bytes": 60, "upload_bytes": 40, "max_download_bytes": 100}
        self.assertEqual(snapshot(row).status, "limited")
        self.assertEqual(snapshot(row).used_traffic_bytes, 100)
        row["max_download_bytes"] = 0
        self.assertEqual(snapshot(row).status, "active")

    def test_day_expiry_is_effective_utc_midnight_not_a_fake_second_expiry(self):
        row = {"expiration_days": 7, "account_creation_date": "2026-11-09"}
        access = snapshot(row, dt.datetime(2026, 11, 15, 23, 59, 59, tzinfo=dt.timezone.utc))
        self.assertEqual(access.status, "active")
        self.assertEqual(access.expires_at, 1794787200)
        self.assertEqual(access.expiry_precision, "day")
        self.assertEqual(access.activation_days, 7)
        self.assertEqual(snapshot(row, dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)).status, "expired")

    def test_unactivated_days_have_no_fake_activation_time(self):
        access = snapshot({"expiration_days": 7, "status": "On-hold"})
        self.assertIsNone(access.expires_at)
        self.assertEqual(access.status, "active")
        self.assertEqual(access.activation_days, 7)

    def test_exact_expiry_and_boolean_integer_rejected_before_any_mutation(self):
        backend = Backend()
        for kwargs in ({"expires_at": 1794787201, "traffic_limit_bytes": 1024},
                       {"expires_at": None, "traffic_limit_bytes": True},
                       {"expires_at": None, "traffic_limit_bytes": 1024, "activation_days": True}):
            with self.assertRaises(BackendConflict):
                backend.create(USER, **kwargs)
        self.assertEqual(backend.collection.rows, {})


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()

    def create(self, **kwargs):
        return self.backend.create(USER, expires_at=None, traffic_limit_bytes=kwargs.pop("limit", 0), **kwargs)

    def test_replayed_create_does_not_rotate_or_reset_existing_usage(self):
        self.create(activation_days=7, limit=1000)
        self.backend.collection.rows[USER].update(download_bytes=30, upload_bytes=20)
        before = copy.deepcopy(self.backend.collection.rows[USER])
        second = self.create(activation_days=7, limit=1000)
        self.assertEqual(second["used_traffic_bytes"], 50)
        self.assertEqual(before, self.backend.collection.rows[USER])
        self.assertEqual(self.backend.auth_calls, [])
        with self.assertRaises(BackendConflict):
            self.create(activation_days=7, limit=1001)
        self.assertEqual(before, self.backend.collection.rows[USER])

    def test_namespace_is_not_sufficient_to_adopt_existing_accounts(self):
        self.backend.collection.rows[USER] = {"_id": USER, "password": "legacy", "max_download_bytes": 99}
        for operation in (lambda: self.backend.get(USER), lambda: self.create(),
                          lambda: self.backend.set_status(USER, "suspended"),
                          lambda: self.backend.revoke(USER), lambda: self.backend.profile(USER)):
            with self.assertRaises(BackendConflict):
                operation()
        self.assertEqual(self.backend.collection.rows[USER]["password"], "legacy")
        with self.assertRaises(BackendConflict):
            self.backend.get("existing_friend")

    def test_offline_suspend_resume_does_not_poison_first_new_flow(self):
        self.create()
        secret = self.backend.collection.rows[USER]["password"]
        self.assertEqual(self.backend.set_status(USER, "suspended")["status"], "suspended")
        self.assertEqual(self.backend.kicks, [])
        self.assertEqual(self.backend.auth_calls[-1], (USER, False))
        self.assertEqual(self.backend.set_status(USER, "active")["status"], "active")
        self.assertEqual(self.backend.collection.rows[USER]["password"], secret)
        self.assertEqual(self.backend.auth_calls, [(USER, False)])
        self.assertTrue(self.backend.first_new_data_succeeds(USER))

    def test_online_suspend_also_avoids_sticky_kick_for_reversible_policy(self):
        self.create()
        self.backend.online[USER] = 1
        self.backend.set_status(USER, "suspended")
        self.backend.set_status(USER, "active")
        self.assertEqual(self.backend.kicks, [])
        self.assertTrue(self.backend.first_new_data_succeeds(USER))

    def test_provision_update_resume_do_not_activate_first_use_account(self):
        self.create(activation_days=7)
        self.backend.update(USER, expires_at=None, traffic_limit_bytes=2048, activation_days=7)
        self.backend.set_status(USER, "active")
        row = self.backend.collection.rows[USER]
        self.assertNotIn("account_creation_date", row)
        self.assertEqual(row["status"], "On-hold")
        self.assertEqual(self.backend.auth_calls, [])

    def test_update_preserves_activation_and_total_counters(self):
        self.create(activation_days=7, limit=1000)
        self.backend.collection.rows[USER].update(account_creation_date="2026-11-09", download_bytes=600, upload_bytes=100)
        secret = self.backend.collection.rows[USER]["password"]
        updated = self.backend.update(USER, expires_at=None, traffic_limit_bytes=500, activation_days=8)
        self.assertEqual(updated["status"], "limited")
        self.assertEqual(updated["used_traffic_bytes"], 700)
        self.assertEqual(self.backend.collection.rows[USER]["password"], secret)
        self.assertEqual(self.backend.collection.rows[USER]["account_creation_date"], "2026-11-09")
        self.assertEqual(self.backend.kicks, [])
        with self.assertRaises(BackendConflict):
            self.backend.set_status(USER, "active")
        self.assertEqual(self.backend.collection.rows[USER]["max_download_bytes"], 500)

    def test_idle_quota_update_and_extension_do_not_poison_first_new_flow(self):
        self.create(limit=1000)
        self.backend.collection.rows[USER].update(download_bytes=600, upload_bytes=100)
        self.backend.update(USER, expires_at=None, traffic_limit_bytes=500)
        self.backend.update(USER, expires_at=None, traffic_limit_bytes=2000)
        self.assertEqual(self.backend.kicks, [])
        self.assertTrue(self.backend.first_new_data_succeeds(USER))

    def test_offline_revoke_does_not_enqueue_a_future_kick(self):
        self.create()
        self.backend.revoke(USER)
        self.backend.revoke(USER)
        self.assertEqual(self.backend.kicks, [])
        self.assertIsNone(self.backend.get(USER))

    def test_online_revoke_uses_only_best_effort_kick_and_still_deletes(self):
        self.create()
        self.backend.online[USER] = 1
        self.backend.kick_unavailable = True
        self.backend.revoke(USER)
        self.assertEqual(self.backend.kicks, [USER])
        self.backend.online[USER] = 0
        self.backend.revoke(USER)
        self.assertIsNone(self.backend.get(USER))

    def test_snapshots_do_not_contain_passwords_or_profile_links(self):
        self.create()
        state = self.backend.get(USER)
        self.assertNotIn(self.backend.collection.rows[USER]["password"], json.dumps(state))
        self.assertNotIn("password", state)
        with self.assertRaises(BackendNotFound):
            self.backend.set_status(OTHER, "suspended")


class WireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "bridge.sock")
        self.backend = Backend()
        self.server = BridgeServer(self.path, BridgeHandler)
        self.server.allowed_uids = {os.getuid()}
        self.server.backend = self.backend
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = HysteriaBackend(self.path, timeout=2)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def test_real_unix_wire_and_metadata_lifecycle(self):
        created = self.client.create(USER, expires_at=None, traffic_limit_bytes=0)
        self.assertEqual(created.status, "active")
        self.assertEqual(created.expiry_precision, "day")
        self.assertEqual(self.client.set_status(USER, "suspended").status, "suspended")
        self.client.revoke(USER)
        self.assertIsNone(self.client.get(USER))

    def test_peer_credentials_reject_unapproved_uid(self):
        self.server.allowed_uids = set()
        with self.assertRaises(BackendUnavailable):
            self.client.get(USER)
        self.assertEqual(self.backend.collection.rows, {})

    def test_dispatch_rejects_extra_commands_and_boolean_version(self):
        for request in ({"version": True, "op": "get", "username": USER},
                        {"version": 1, "op": "get", "username": USER, "command": "delete_all"},
                        {"version": 1, "op": "shell", "username": USER}):
            with self.assertRaises(BackendConflict):
                dispatch(self.backend, request)


class InternalHTTPTests(unittest.TestCase):
    def test_internal_redirect_is_rejected_before_second_request(self):
        visited = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                visited.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", "/must-not-receive-secrets")
                self.end_headers()

            def do_GET(self):
                visited.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        backend = LegacyHysteria(Collection(), "/unused", public_host="example.invalid")
        try:
            with self.assertRaises(BackendUnavailable):
                backend._request("http://127.0.0.1:" + str(server.server_port) + "/redirect",
                                 payload={"auth": "private"}, secret="private")
            self.assertEqual(visited, ["/redirect"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_health_does_not_authenticate_an_existing_account(self):
        backend = Backend()
        backend.create(USER, expires_at=None, traffic_limit_bytes=0, activation_days=7)
        before = copy.deepcopy(backend.collection.rows)
        requests = []

        class Database:
            def command(self, value):
                if value != "ping":
                    raise AssertionError("unexpected Mongo command")

        backend.collection.database = Database()
        backend._config = lambda: {"trafficStats": {"listen": "127.0.0.1:25413", "secret": "private"},
                                    "auth": {"http": {"url": "http://127.0.0.1:28262/auth"}}}

        def request(url, **kwargs):
            requests.append((url, kwargs))
            return {"ok": False} if url.endswith("/auth") else {}

        backend._request = request
        self.assertTrue(backend.health()["ready"])
        auth = requests[-1][1]["payload"]["auth"]
        self.assertNotEqual(auth.split(":", 1)[0], USER)
        self.assertEqual(backend.collection.rows, before)


class ProfileCertificateTests(unittest.TestCase):
    def test_delivered_public_certificate_works_when_legacy_path_is_unreadable(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cert = directory / "public-certificate.pem"
            key = directory / "test-private-key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=example.invalid"], check=True, capture_output=True)
            cert.chmod(0o600)
            config = directory / "server.json"
            config.write_text(json.dumps({"tls": {"cert": "/unreadable-engine-owned/cert.pem"},
                                          "obfs": {"type": "salamander", "salamander": {"password": "obfs-secret"}}}))
            collection = Collection()
            collection.insert_one({"_id": USER, "note": marker(USER), "password": "private-password",
                                   "max_download_bytes": 0, "expiration_days": 0})
            backend = LegacyHysteria(collection, config, public_host="example.invalid",
                                     certificate_file=cert)
            uri = urlsplit(backend.profile(USER))
            query = parse_qs(uri.query)
            expected = subprocess.run(["openssl", "x509", "-in", str(cert), "-outform", "DER"],
                                      check=True, capture_output=True).stdout
            self.assertEqual(query["pinSHA256"], [hashlib.sha256(expected).hexdigest()])
            self.assertEqual(query["obfs-password"], ["obfs-secret"])
            self.assertEqual(uri.hostname, "example.invalid")


if __name__ == "__main__":
    unittest.main()
