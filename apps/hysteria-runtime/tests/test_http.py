import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hysteria_runtime.runtime import BoundedServer, RuntimeStateError, collect_once, engine_caps, engine_close_caps, load_config, make_handler


class FakeCollection:
    def __init__(self):
        self.reads = 0
        self.fail = False

    def find_one(self, query):
        self.reads += 1
        if self.fail:
            raise OSError("private database address must not leak")
        return {"_id": "sample", "password": "private-secret", "expiration_days": 7}


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.collection = FakeCollection()
        self.ping_fail = False
        self.engine_checker = lambda: True
        self.engine_checks = 0
        self.close_checker = lambda: True

        def ping():
            if self.ping_fail:
                raise OSError("private Mongo connection details")

        def engine_supported():
            self.engine_checks += 1
            return self.engine_checker()

        self.server = BoundedServer(("127.0.0.1", 0),
                                    make_handler(self.collection, ping, engine_supported=engine_supported,
                                                 close_supported=lambda: self.close_checker()))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None, port=None):
        client = http.client.HTTPConnection("127.0.0.1", port or self.server.server_port, timeout=3)
        try:
            client.request(method, path, body=body, headers=headers or {})
            response = client.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            client.close()

    def test_auth_and_health_do_not_activate_or_disclose_secret(self):
        status, headers, raw = self.request("POST", "/auth", json.dumps({"auth": "sample:private-secret"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "id": "sample"})
        self.assertNotIn(b"private-secret", raw)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.collection.reads, 2)
        self.assertEqual(self.request("GET", "/readyz")[0], 200)
        self.assertEqual(self.collection.reads, 2)

    def test_database_failure_is_fail_closed_without_exception_text(self):
        self.collection.fail = True
        status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"ok": False})
        self.assertNotIn(b"database", raw)

    def test_capabilities_report_policy_support_without_authentication_or_mongo_read(self):
        self.collection.fail = True
        status, headers, raw = self.request("GET", "/capabilities")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"max_sessions": True, "version": 1,
                                          "policy_source": "mongo_max_sessions",
                                          "quota_expiry_enforcement": True, "policy_close_fence": True,
                                          "enforcement_version": 1})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.collection.reads, 0)
        self.assertNotIn(b"private-secret", raw)

    def test_http_auth_returns_session_policy_and_does_not_echo_credentials(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}
        status, _, raw = self.request("POST", "/auth", json.dumps({"auth": "sample:private-secret"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "id": "sample", "max_sessions": 1})
        self.assertNotIn(b"private-secret", raw)

    def test_http_invalid_session_policy_is_a_clean_denial(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": True}
        status, _, raw = self.request("POST", "/auth", json.dumps({"auth": "sample:private-secret"}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": False})
        self.assertNotIn(b"private-secret", raw)

    def test_limited_auth_requires_exact_true_from_engine_checker(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}
        for value in (False, None, 0, 1, "true", {}, []):
            with self.subTest(value=value):
                self.engine_checker = lambda: value
                status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": False})
                self.assertNotIn(b"private-secret", raw)

    def test_limited_auth_checker_exception_is_a_clean_denial(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}

        def broken_checker():
            raise OSError("private engine secret and address")

        self.engine_checker = broken_checker
        status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": False})
        self.assertNotIn(b"private", raw)

    def test_limited_auth_without_engine_checker_is_denied(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}
        with BoundedServer(("127.0.0.1", 0), make_handler(self.collection, lambda: None)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}',
                                               port=server.server_port)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"ok": False})
            finally:
                server.shutdown()
                thread.join(2)
                self.assertFalse(thread.is_alive())

    def test_unlimited_auth_does_not_call_engine_checker(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret"}
        def unexpected_checker():
            raise AssertionError("unlimited auth must not depend on session enforcement")

        self.engine_checker = unexpected_checker
        status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "id": "sample"})
        self.assertEqual(self.engine_checks, 0)

    def test_finite_quota_and_day_policy_require_actual_close_support(self):
        for policy in ({"max_download_bytes": 100}, {"expiration_days": 7}):
            self.collection.find_one = lambda query, policy=policy: {"_id": "sample", "password": "private-secret", **policy}
            for supported in (True, False, None, 1, "true", True):
                self.close_checker = lambda supported=supported: supported
                status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
                self.assertEqual(status, 200)
                self.assertIs(json.loads(raw)["ok"], supported is True)

    def test_pending_policy_fence_denies_even_after_quota_extension(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret",
                                                  "unlimited_user": True, "_vpn_policy_close_pending": None}
        self.assertEqual(json.loads(self.request("POST", "/auth", '{"auth":"sample:private-secret"}')[2]), {"ok": False})

    def test_engine_checker_is_rechecked_after_a_downgrade(self):
        self.collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}
        for supported in (True, False, True):
            with self.subTest(supported=supported):
                self.engine_checker = lambda: supported
                status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["ok"], supported)
        self.assertEqual(self.engine_checks, 3)

    def test_malformed_or_oversize_body_never_queries_mongo(self):
        self.assertEqual(self.request("POST", "/auth", "not json")[0], 400)
        self.assertEqual(self.request("POST", "/auth", "x" * 16385)[0], 413)
        self.assertEqual(self.request("POST", "/auth", "[]")[0], 400)
        self.assertEqual(self.collection.reads, 0)

    def test_chunked_body_is_rejected_before_processing(self):
        status, _, _ = self.request("POST", "/auth", "0\r\n\r\n", {"Transfer-Encoding": "chunked"})
        self.assertEqual(status, 400)
        self.assertEqual(self.collection.reads, 0)

    def test_readyz_reports_dependency_failure_but_liveness_remains(self):
        self.ping_fail = True
        status, _, raw = self.request("GET", "/readyz")
        self.assertEqual(status, 503)
        self.assertNotIn(b"Mongo", raw)
        self.assertEqual(self.request("GET", "/healthz")[0], 200)


class EngineCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = {"max_sessions": True, "session_scope": "auth_id_per_server_instance",
                             "enforcement": "atomic_quic_connection", "version": 1}
        self.body = self.capabilities
        self.status = 200
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "private-stats-secret":
                    self.send_response(401)
                    self.end_headers()
                    return
                raw = fixture.body if isinstance(fixture.body, bytes) else json.dumps(fixture.body).encode()
                self.send_response(fixture.status)
                self.send_header("Content-Length", str(len(raw)))
                if fixture.status == 302:
                    self.send_header("Location", "/must-not-receive-stats-secret")
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {"stats_url": "http://127.0.0.1:" + str(self.server.server_port),
                       "stats_secret": "private-stats-secret"}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def test_actual_protected_endpoint_is_checked_with_its_stats_secret(self):
        self.assertIs(engine_caps(self.config), True)
        self.assertEqual(self.requests, [("/capabilities", "private-stats-secret")])

    def test_missing_or_malformed_capability_is_unsupported(self):
        invalid = [{}, [], {**self.capabilities, "max_sessions": 1},
                   {**self.capabilities, "max_sessions": False},
                   {**self.capabilities, "session_scope": "auth_id_per_server_process"},
                   {**self.capabilities, "enforcement": "poll_and_kick"},
                   {**self.capabilities, "version": True}, {**self.capabilities, "version": "1"},
                   {**self.capabilities, "version": 2}, b"not json"]
        for value in invalid:
            with self.subTest(value=value):
                self.body = value
                self.assertIs(engine_caps(self.config), False)

    def test_legacy_404_wrong_secret_and_redirect_fail_closed(self):
        self.status = 404
        self.assertIs(engine_caps(self.config), False)
        self.status = 200
        self.assertIs(engine_caps({**self.config, "stats_secret": "wrong-secret"}), False)
        self.requests.clear()
        self.status = 302
        self.assertIs(engine_caps(self.config), False)
        self.assertEqual(self.requests, [("/capabilities", "private-stats-secret")])

    def test_real_stats_downgrade_denies_existing_limited_account(self):
        collection = FakeCollection()
        collection.find_one = lambda query: {"_id": "sample", "password": "private-secret", "max_sessions": 1}
        handler = make_handler(collection, lambda: None, engine_supported=lambda: engine_caps(self.config))
        with BoundedServer(("127.0.0.1", 0), handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for status, expected in ((200, True), (404, False), (200, True)):
                    self.status = status
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    try:
                        client.request("POST", "/auth", body='{"auth":"sample:private-secret"}')
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertIs(json.loads(response.read())["ok"], expected)
                    finally:
                        client.close()
            finally:
                server.shutdown()
                thread.join(2)
                self.assertFalse(thread.is_alive())


class IOTests(unittest.TestCase):
    def test_bad_accounting_cursor_does_not_starve_another_expired_account(self):
        from test_runtime import MemoryCollection, account
        collection = MemoryCollection(account(_id="bad-cursor", _vpn_collector=None),
                                      account(_id="already-expired", expiration_days=1,
                                              account_creation_date="2020-01-01"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "epoch"
            path.write_text("epoch1")
            with patch("hysteria_runtime.runtime.fetch_stats", return_value={}), \
                    patch("hysteria_runtime.runtime.close_sessions", return_value={"closed": 1}) as close:
                with self.assertRaisesRegex(RuntimeStateError, "collector_cursor_invalid"):
                    collect_once(collection, {"epoch_file": str(path)})
            close.assert_called_once_with({"epoch_file": str(path)}, ["already-expired"])
            self.assertNotIn("_vpn_policy_close_pending", collection.documents["already-expired"])

    def test_config_rejects_public_permissions_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"stats_secret":"sample"}')
            path.chmod(0o644)
            with self.assertRaisesRegex(RuntimeStateError, "config_must_be_private"):
                load_config(path)
            path.chmod(0o600)
            self.assertEqual(load_config(path)["stats_secret"], "sample")
            link = path.with_name("link.json")
            link.symlink_to(path)
            with self.assertRaises(OSError):
                load_config(link)

    def test_server_restart_during_snapshot_does_not_write_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "epoch"
            path.write_text("epoch1")
            collection = object()

            def fetch(config, suffix):
                if suffix == "/online":
                    path.write_text("epoch2")
                return {}

            with patch("hysteria_runtime.runtime.fetch_stats", fetch), patch("hysteria_runtime.runtime.apply_snapshot") as apply:
                with self.assertRaisesRegex(RuntimeStateError, "collector_epoch_changed"):
                    collect_once(collection, {"epoch_file": str(path)})
                apply.assert_not_called()

    def test_collector_uses_only_read_only_statistics_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "epoch"
            path.write_text("epoch1")
            calls = []

            def fetch(config, suffix):
                calls.append(suffix)
                return {}

            with patch("hysteria_runtime.runtime.fetch_stats", fetch), patch("hysteria_runtime.runtime.apply_snapshot", return_value={"updated": 0}):
                collect_once(object(), {"epoch_file": str(path)}, accounting_only=True)
            self.assertEqual(calls, ["/traffic?clear=false", "/online"])


if __name__ == "__main__":
    unittest.main()
