"""The bridge must preserve runtime-owned quota/expiry close fences."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vpn_control.common import BackendUnavailable
from vpn_control.hys_bridge import snapshot
from test_hysteria import Backend, USER


PENDING = "_vpn_policy_close_pending"


class RuntimePolicyFenceTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.backend.create(USER)
        self.backend.collection.rows[USER].update(
            upload_bytes=17,
            download_bytes=23,
            account_creation_date="2026-11-09",
        )
        self.marker = {"token": "a" * 32, "reason": "quota", "policy": "b" * 64}

    def mark_pending(self, value=None):
        self.backend.collection.rows[USER][PENDING] = (
            deepcopy(self.marker) if value is None else deepcopy(value)
        )

    def test_any_present_runtime_fence_is_reported_as_suspended(self):
        for value in (self.marker, None, {}, [], False, "malformed"):
            with self.subTest(value=value):
                self.backend.collection.rows[USER][PENDING] = deepcopy(value)
                before = deepcopy(self.backend.collection.rows[USER])
                self.assertEqual(snapshot(before).status, "suspended")
                self.assertEqual(self.backend.get(USER)["status"], "suspended")
                self.assertEqual(self.backend.collection.rows[USER], before)

    def test_activation_cannot_clear_or_bypass_runtime_owned_pending_close(self):
        self.mark_pending()
        before = deepcopy(self.backend.collection.rows)
        with self.assertRaisesRegex(BackendUnavailable, "hysteria_policy_close_pending"):
            self.backend.set_status(USER, "active")
        self.assertEqual(self.backend.collection.rows, before)

    def test_profile_export_fails_before_reading_configuration_or_certificate(self):
        self.mark_pending()
        before = deepcopy(self.backend.collection.rows)
        self.backend._config = lambda: self.fail("pending profile must not read configuration")
        with self.assertRaisesRegex(BackendUnavailable, "hysteria_policy_close_pending"):
            self.backend.profile(USER)
        self.assertEqual(self.backend.collection.rows, before)

    def test_quota_extension_preserves_identity_usage_manual_flags_and_fence(self):
        self.mark_pending()
        row = self.backend.collection.rows[USER]
        row["blocked"] = True
        row["unlimited_user"] = True
        before = deepcopy(row)
        result = self.backend.update(USER, traffic_limit_bytes=1_000_000,
                                     activation_days=7)
        current = self.backend.collection.rows[USER]
        self.assertEqual(result["status"], "suspended")
        for field in ("password", "upload_bytes", "download_bytes", "blocked",
                      "unlimited_user", "account_creation_date", PENDING):
            self.assertEqual(current[field], before[field])
        self.assertEqual(current["max_download_bytes"], 1_000_000)
        with self.assertRaisesRegex(BackendUnavailable, "hysteria_policy_close_pending"):
            self.backend.set_status(USER, "active")

    def test_replayed_creation_preserves_runtime_fence_and_original_credentials(self):
        self.mark_pending()
        before = deepcopy(self.backend.collection.rows)
        result = self.backend.create(USER)
        self.assertEqual(result["status"], "suspended")
        self.assertEqual(self.backend.collection.rows, before)

    def test_suspension_remains_available_and_does_not_clear_runtime_fence(self):
        self.mark_pending()
        before = deepcopy(self.backend.collection.rows[USER])
        closed = []
        self.backend._close_connections = lambda name, **kwargs: closed.append(name) or True
        result = self.backend.set_status(USER, "suspended")
        current = self.backend.collection.rows[USER]
        self.assertEqual(result["status"], "suspended")
        self.assertTrue(current["blocked"])
        self.assertEqual(current[PENDING], before[PENDING])
        self.assertEqual(current["password"], before["password"])
        self.assertEqual((current["upload_bytes"], current["download_bytes"]), (17, 23))
        self.assertEqual(closed, [USER])

    def test_runtime_acknowledgement_allows_activation_and_profile_again(self):
        self.mark_pending()
        with self.assertRaises(BackendUnavailable):
            self.backend.set_status(USER, "active")
        # Only the collector acknowledges/removes this marker after /close.
        del self.backend.collection.rows[USER][PENDING]
        result = self.backend.set_status(USER, "active")
        self.assertEqual(result["status"], "active")
        self.backend._config = lambda: {
            "tls": {"cert": "/unused-public-fixture-certificate.pem"},
            "obfs": {"type": "salamander", "salamander": {"password": "fixture-obfs"}},
        }
        with patch("vpn_control.hys_bridge.subprocess.run",
                   return_value=SimpleNamespace(stdout=b"fixture-public-certificate-der")):
            self.assertTrue(self.backend.profile(USER).startswith("hysteria2://"))


if __name__ == "__main__":
    unittest.main()
