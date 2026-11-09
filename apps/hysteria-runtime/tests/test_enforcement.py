"""Fault-driven checks for policy denial and closing established QUIC sessions."""

from copy import deepcopy
import datetime as dt
import unittest

from hysteria_runtime.runtime import (
    RuntimeStateError,
    authorize,
    enforce_policies,
    policy_denial,
)
from test_runtime import MemoryCollection, NOW, USER, account


PENDING = "_vpn_policy_close_pending"
ACK = "_vpn_policy_closed"
EPOCH = "test-engine-epoch"


def denied_account(**changes):
    return account(
        max_download_bytes=100,
        upload_bytes=40,
        download_bytes=60,
        **changes,
    )


def run_enforcement(collection, closer, epoch=EPOCH):
    """A failed external/DB acknowledgement may be reported as unavailable."""
    try:
        return enforce_policies(collection, epoch, closer, now=NOW)
    except RuntimeStateError:
        return None


def original_fields(row):
    return {
        key: deepcopy(value)
        for key, value in row.items()
        if key not in {PENDING, ACK}
    }


class PolicyDenialTests(unittest.TestCase):
    def test_quota_boundary_sums_both_directions(self):
        self.assertEqual(policy_denial(denied_account(), now=NOW), "quota")
        self.assertIsNone(policy_denial(account(max_download_bytes=100,
            upload_bytes=40, download_bytes=59), now=NOW))

    def test_expiry_is_utc_and_applies_without_new_traffic(self):
        row = account(expiration_days=7, account_creation_date="2026-11-02")
        self.assertIsNone(policy_denial(row, now=dt.datetime(2026, 11, 8, 23, 59, 59,
                                                           tzinfo=dt.timezone.utc)))
        self.assertEqual(policy_denial(row, now=NOW), "expired")

    def test_absent_activation_date_is_not_started_by_a_policy_check(self):
        row = account(expiration_days=7)
        before = deepcopy(row)
        self.assertIsNone(policy_denial(row, now=NOW))
        self.assertEqual(row, before)

    def test_legacy_null_and_empty_dates_keep_the_first_use_hold_policy(self):
        for value in (None, ""):
            with self.subTest(value=value):
                row = account(expiration_days=7, account_creation_date=value)
                collection = MemoryCollection(row)
                before = deepcopy(collection.documents)
                self.assertIsNone(policy_denial(row, now=NOW))
                self.assertEqual(authorize(collection, USER + ":test-password:with-colon", now=NOW),
                                 {"ok": True, "id": USER})
                def unexpected_close(_ids):
                    self.fail("An unactivated legacy account must not be closed")
                self.assertEqual(enforce_policies(collection, EPOCH, unexpected_close, now=NOW),
                                 {"observed": 1, "closed": 0, "pending": 0})
                self.assertEqual(collection.documents, before)
                self.assertEqual(collection.updates, [])

    def test_malformed_nonempty_finite_date_fails_closed_without_rewriting_it(self):
        for value in (False, 42, [], {}, "invalid", "2026-13-99"):
            with self.subTest(value=value):
                row = account(expiration_days=7, account_creation_date=value)
                before = deepcopy(row)
                self.assertEqual(policy_denial(row, now=NOW), "invalid_policy")
                self.assertEqual(row, before)

    def test_unlimited_waives_quota_expiry_but_does_not_override_manual_block(self):
        row = denied_account(unlimited_user=True, expiration_days=1,
                             account_creation_date="2020-01-01")
        self.assertIsNone(policy_denial(row, now=NOW))
        row["blocked"] = True
        self.assertEqual(policy_denial(row, now=NOW), "blocked")

    def test_bools_and_non_integer_usage_or_limits_are_invalid_policies(self):
        for fields in ({"max_download_bytes": True}, {"expiration_days": False},
                       {"upload_bytes": True}, {"download_bytes": -1},
                       {"max_download_bytes": "100"}):
            with self.subTest(fields=fields):
                self.assertEqual(policy_denial(account(**fields), now=NOW),
                                 "invalid_policy")


class EnforcementLifecycleTests(unittest.TestCase):
    def test_durable_pending_fences_auth_before_close_and_is_acknowledged(self):
        collection = MemoryCollection(denied_account(
            _vpn_collector={"epoch": EPOCH, "tx": 40, "rx": 60, "seq": 8},
            max_sessions=1,
        ))
        before = original_fields(collection.documents[USER])
        calls = []

        def close(ids):
            calls.append(ids)
            marker = collection.documents[USER][PENDING]
            self.assertIsInstance(marker["token"], str)
            self.assertEqual(marker["reason"], "quota")
            self.assertIsInstance(marker["policy"], str)
            self.assertEqual(authorize(collection, USER + ":test-password:with-colon", now=NOW),
                             {"ok": False})
            return {"closed": 1}

        result = enforce_policies(collection, EPOCH, close, now=NOW)
        self.assertEqual(calls, [[USER]])
        self.assertEqual(result["observed"], 1)
        self.assertEqual(result["closed"], 1)
        self.assertEqual(result["pending"], 0)
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertEqual(collection.documents[USER][ACK]["epoch"], EPOCH)
        self.assertEqual(original_fields(collection.documents[USER]), before)

    def test_success_is_not_reclosed_until_engine_epoch_changes(self):
        collection = MemoryCollection(denied_account())
        calls = []

        def close(ids):
            calls.append(ids)
            return {"closed": 1}

        enforce_policies(collection, EPOCH, close, now=NOW)
        enforce_policies(collection, EPOCH, close, now=NOW)
        self.assertEqual(calls, [[USER]])
        enforce_policies(collection, EPOCH + "-after-restart", close, now=NOW)
        self.assertEqual(calls, [[USER], [USER]])

    def test_new_collector_sequence_rechecks_previously_closed_policy(self):
        collection = MemoryCollection(denied_account(
            _vpn_collector={"epoch": EPOCH, "tx": 40, "rx": 60, "seq": 8}))
        calls = []
        close = lambda ids: calls.append(ids) or {"closed": 1}
        enforce_policies(collection, EPOCH, close, now=NOW)
        collection.documents[USER]["_vpn_collector"]["seq"] += 1
        enforce_policies(collection, EPOCH, close, now=NOW)
        self.assertEqual(calls, [[USER], [USER]])

    def test_observed_valid_policy_clears_old_ack_before_a_later_denial(self):
        collection = MemoryCollection(denied_account())
        calls = []
        close = lambda ids: calls.append(ids) or {"closed": 1}
        enforce_policies(collection, EPOCH, close, now=NOW)
        collection.documents[USER]["max_download_bytes"] = 1000
        enforce_policies(collection, EPOCH, close, now=NOW)
        self.assertNotIn(ACK, collection.documents[USER])
        collection.documents[USER]["max_download_bytes"] = 100
        enforce_policies(collection, EPOCH, close, now=NOW)
        self.assertEqual(calls, [[USER], [USER]])

    def test_close_failure_keeps_pending_auth_denied_and_retry_finishes_it(self):
        collection = MemoryCollection(denied_account())
        before = original_fields(collection.documents[USER])

        def unavailable(ids):
            self.assertIn(PENDING, collection.documents[USER])
            raise RuntimeStateError("test_close_unavailable")

        run_enforcement(collection, unavailable)
        self.assertIn(PENDING, collection.documents[USER])
        self.assertEqual(original_fields(collection.documents[USER]), before)
        enforce_policies(collection, EPOCH, lambda ids: {"closed": 1}, now=NOW)
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertEqual(original_fields(collection.documents[USER]), before)

    def test_pending_close_still_fences_a_policy_which_became_unlimited(self):
        collection = MemoryCollection(denied_account())

        def first_close(ids):
            collection.documents[USER]["unlimited_user"] = True
            self.assertEqual(authorize(collection, USER + ":test-password:with-colon", now=NOW),
                             {"ok": False})
            raise RuntimeStateError("test_close_unavailable")

        run_enforcement(collection, first_close)
        self.assertIn(PENDING, collection.documents[USER])
        calls = []

        def retry_close(ids):
            calls.append(ids)
            return {"closed": 0}

        enforce_policies(collection, EPOCH, retry_close, now=NOW)
        self.assertEqual(calls, [[USER]])
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertTrue(authorize(collection, USER + ":test-password:with-colon", now=NOW)["ok"])

    def test_extension_before_marker_write_avoids_a_stale_close(self):
        collection = MemoryCollection(denied_account())
        collection.before_update = lambda database: database.documents[USER].update(
            max_download_bytes=1000)
        calls = []
        enforce_policies(collection, EPOCH,
                         lambda ids: calls.append(ids) or {"closed": 1}, now=NOW)
        self.assertEqual(calls, [])
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertEqual(collection.documents[USER]["max_download_bytes"], 1000)

    def test_enabling_unlimited_before_marker_write_does_not_close_new_policy(self):
        collection = MemoryCollection(denied_account())
        collection.before_update = lambda database: database.documents[USER].update(
            unlimited_user=True)
        calls = []
        enforce_policies(collection, EPOCH,
                         lambda ids: calls.append(ids) or {"closed": 1}, now=NOW)
        self.assertEqual(calls, [])
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertTrue(collection.documents[USER]["unlimited_user"])

    def test_lost_close_ack_is_retried_without_adding_or_resetting_usage(self):
        collection = MemoryCollection(denied_account())
        before = original_fields(collection.documents[USER])
        calls = []

        def acknowledged_but_reply_lost(ids):
            calls.append(ids)
            raise RuntimeStateError("test_reply_lost_after_real_close")

        run_enforcement(collection, acknowledged_but_reply_lost)
        self.assertIn(PENDING, collection.documents[USER])
        enforce_policies(collection, EPOCH,
                         lambda ids: calls.append(ids) or {"closed": 0}, now=NOW)
        self.assertEqual(calls, [[USER], [USER]])
        self.assertEqual(original_fields(collection.documents[USER]), before)

    def test_lost_database_ack_after_close_replays_the_persisted_ack(self):
        collection = MemoryCollection(denied_account())
        calls = []

        def close(ids):
            calls.append(ids)
            collection.fail_after_commit = True
            return {"closed": 1}

        run_enforcement(collection, close)
        self.assertNotIn(PENDING, collection.documents[USER])
        self.assertIn(ACK, collection.documents[USER])
        enforce_policies(collection, EPOCH,
                         lambda ids: self.fail("committed close acknowledgement must replay"),
                         now=NOW)
        self.assertEqual(calls, [[USER]])

    def test_old_response_cannot_clear_a_new_pending_token(self):
        collection = MemoryCollection(denied_account())
        new_token = "f" * 32
        calls = []

        def close(ids):
            calls.append(ids)
            if len(calls) > 1:
                raise RuntimeStateError("hold_new_pending")
            marker = collection.documents[USER][PENDING]
            collection.documents[USER][PENDING] = {**marker, "token": new_token}
            return {"closed": 1}

        run_enforcement(collection, close)
        self.assertEqual(collection.documents[USER][PENDING]["token"], new_token)

    def test_invalid_close_response_never_removes_pending_auth_fence(self):
        for response in ({}, {"closed": True}, {"closed": -1}, {"closed": "1"}, None, []):
            with self.subTest(response=response):
                collection = MemoryCollection(denied_account())
                run_enforcement(collection, lambda ids: response)
                self.assertIn(PENDING, collection.documents[USER])

    def test_one_failed_close_does_not_starve_another_expired_account(self):
        second = "second-policy-user"
        collection = MemoryCollection(denied_account(), denied_account(_id=second))
        calls = []

        def close(ids):
            calls.append(ids)
            if ids == [USER]:
                raise RuntimeStateError("first_user_unavailable")
            return {"closed": 1}

        run_enforcement(collection, close)
        self.assertEqual(calls, [[USER], [second]])
        self.assertIn(PENDING, collection.documents[USER])
        self.assertNotIn(PENDING, collection.documents[second])
        self.assertIn(ACK, collection.documents[second])

    def test_malformed_pending_fence_denies_auth_even_for_unlimited_accounts(self):
        for pending in (None, {}, [], False, "invalid"):
            with self.subTest(pending=pending):
                collection = MemoryCollection(account(unlimited_user=True,
                    **{PENDING: pending}))
                before = deepcopy(collection.documents)
                self.assertEqual(authorize(collection, USER + ":test-password:with-colon", now=NOW),
                                 {"ok": False})
                self.assertEqual(collection.documents, before)

    def test_idle_expired_account_is_closed_even_without_accounting_activity(self):
        collection = MemoryCollection(account(expiration_days=1,
            account_creation_date="2026-11-01", online_count=0))
        calls = []
        result = enforce_policies(collection, EPOCH,
            lambda ids: calls.append(ids) or {"closed": 0}, now=NOW)
        self.assertEqual(calls, [[USER]])
        self.assertEqual(result["pending"], 0)

    def test_policy_close_does_not_clear_bridge_tightening_marker(self):
        collection = MemoryCollection(denied_account(_vpn_session_close_pending=True))
        enforce_policies(collection, EPOCH, lambda ids: {"closed": 1}, now=NOW)
        self.assertIs(collection.documents[USER]["_vpn_session_close_pending"], True)
        self.assertNotIn(PENDING, collection.documents[USER])

    def test_no_denial_causes_no_writes_or_remote_requests(self):
        collection = MemoryCollection(account(max_download_bytes=1000,
            upload_bytes=40, download_bytes=60))
        before = deepcopy(collection.documents)
        result = enforce_policies(collection, EPOCH,
            lambda ids: self.fail("valid policy must not close sessions"), now=NOW)
        self.assertEqual(result, {"observed": 1, "closed": 0, "pending": 0})
        self.assertEqual(collection.documents, before)
        self.assertEqual(collection.updates, [])


if __name__ == "__main__":
    unittest.main()
