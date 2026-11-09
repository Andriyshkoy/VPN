import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from serve_native_fixture import local_mongo_uri, native_engine_close_supported, native_engine_supported, write_ready


class NativeFixtureSafetyTests(unittest.TestCase):
    def test_uri_accepts_only_direct_loopback_mongo(self):
        for uri in ("mongodb://127.0.0.1:27017/", "mongodb://[::1]:27017/",
                    "mongodb://localhost/", "mongodb://name:pass@127.0.0.1/?authSource=admin"):
            with self.subTest(uri=uri):
                self.assertEqual(local_mongo_uri(uri), uri)

    def test_uri_rejects_remote_hosts_discovery_proxy_and_existing_database(self):
        for uri in ("mongodb://production.example/", "mongodb://192.0.2.1/",
                    "mongodb+srv://127.0.0.1/", "mongodb://127.0.0.1,192.0.2.1/",
                    "mongodb://127.0.0.1/existing", "mongodb://127.0.0.1/?replicaSet=remote",
                    "mongodb://127.0.0.1/?proxyHost=remote", "mongodb://127.0.0.1/?directConnection=false",
                    "mongodb://127.0.0.1/?authSource=admin&authSource=other"):
            with self.subTest(uri=uri):
                with self.assertRaisesRegex(ValueError, "fixture_requires_loopback_mongo_uri"):
                    local_mongo_uri(uri)

    def test_ready_file_is_private_and_valid_json_after_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready.json"
            ready.write_text("old")
            ready.chmod(0o644)
            metadata = {"database": "vpn_hys_native_public_fixture"}
            write_ready(ready, metadata)
            self.assertEqual(json.loads(ready.read_text()), metadata)
            self.assertEqual(stat.S_IMODE(ready.stat().st_mode), 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [ready])


    def test_engine_support_requires_current_valid_ready_file(self):
        with patch.dict(os.environ, {}, clear=True), patch("serve_native_fixture.engine_caps") as check:
            self.assertIs(native_engine_supported(), False)
            with tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / "engine.json"
                os.environ["HYS_NATIVE_ENGINE_READY"] = str(ready)
                self.assertIs(native_engine_supported(), False)
                for raw in ("not json", "[]", "{}", '{"stats_url":"http://127.0.0.1:1"}', "x" * 4097):
                    ready.write_text(raw)
                    self.assertIs(native_engine_supported(), False)
            check.assert_not_called()

    def test_engine_ready_is_reread_and_checked_for_each_limited_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "engine.json"
            first = {"stats_url": "http://127.0.0.1:12345", "stats_secret": "first-public-fixture-secret"}
            second = {"stats_url": "http://127.0.0.1:23456", "stats_secret": "second-public-fixture-secret"}
            with patch.dict(os.environ, {"HYS_NATIVE_ENGINE_READY": str(ready)}, clear=True), \
                    patch("serve_native_fixture.engine_caps", side_effect=(True, False)) as check:
                write_ready(ready, first)
                self.assertIs(native_engine_supported(), True)
                write_ready(ready, second)
                self.assertIs(native_engine_supported(), False)
                self.assertEqual([call.args[0] for call in check.call_args_list], [first, second])

    def test_close_support_requires_the_actual_current_engine_ready_endpoint(self):
        with patch.dict(os.environ, {}, clear=True), patch("serve_native_fixture.engine_close_caps") as check:
            self.assertIs(native_engine_close_supported(), False)
            check.assert_not_called()
            with tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / "engine.json"
                os.environ["HYS_NATIVE_ENGINE_READY"] = str(ready)
                first = {"stats_url": "http://127.0.0.1:12345", "stats_secret": "first-public-fixture-secret"}
                second = {"stats_url": "http://127.0.0.1:23456", "stats_secret": "second-public-fixture-secret"}
                check.side_effect = (True, False)
                write_ready(ready, first)
                self.assertIs(native_engine_close_supported(), True)
                write_ready(ready, second)
                self.assertIs(native_engine_close_supported(), False)
                self.assertEqual([call.args[0] for call in check.call_args_list], [first, second])


if __name__ == "__main__":
    unittest.main()
