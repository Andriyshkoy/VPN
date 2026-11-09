import contextlib
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from hysteria_runtime.runtime import BoundedServer, RuntimeStateError, collect_once, load_config, make_handler


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

        def ping():
            if self.ping_fail:
                raise OSError("private Mongo connection details")

        self.server = BoundedServer(("127.0.0.1", 0), make_handler(self.collection, ping))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
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
        self.assertEqual(self.collection.reads, 1)
        self.assertEqual(self.request("GET", "/readyz")[0], 200)
        self.assertEqual(self.collection.reads, 1)

    def test_database_failure_is_fail_closed_without_exception_text(self):
        self.collection.fail = True
        status, _, raw = self.request("POST", "/auth", '{"auth":"sample:private-secret"}')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"ok": False})
        self.assertNotIn(b"database", raw)

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


class IOTests(unittest.TestCase):
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
                collect_once(object(), {"epoch_file": str(path)})
            self.assertEqual(calls, ["/traffic?clear=false", "/online"])


if __name__ == "__main__":
    unittest.main()
