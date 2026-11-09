"""Behavioral regressions for legacy-compatible auth and cumulative accounting."""
from copy import deepcopy
import datetime as dt
from types import SimpleNamespace
import unittest

from hysteria_runtime.runtime import RuntimeStateError, apply_snapshot, authorize


MISSING = object()
NOW = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)
TODAY = NOW.date()
USER = "runtime_canary"


def value_at(document, dotted):
    value = document
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def set_at(document, dotted, value):
    parts = dotted.split(".")
    target = document
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = deepcopy(value)


def matches(document, query):
    for key, expected in query.items():
        if key == "$or":
            if not any(matches(document, alternative) for alternative in expected):
                return False
            continue
        if key == "$and":
            if not all(matches(document, alternative) for alternative in expected):
                return False
            continue
        actual = value_at(document, key)
        if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
            for operation, operand in expected.items():
                if operation == "$exists" and (actual is not MISSING) != operand:
                    return False
                if operation == "$eq" and actual != operand:
                    return False
                if operation == "$ne" and actual == operand:
                    return False
        elif actual is MISSING and expected is None:
            continue  # MongoDB equality with null also selects a missing field.
        elif actual != expected:
            return False
    return True


class MemoryCollection:
    """Copies documents on read and applies atomic conditional updates."""

    def __init__(self, *documents):
        self.documents = {document["_id"]: deepcopy(document) for document in documents}
        self.updates = []
        self.before_update = None
        self.fail_next_update = False
        self.fail_after_commit = False
        self.fail_reads = False

    def find(self, query=None, *args, **kwargs):
        if self.fail_reads:
            raise RuntimeError("simulated_database_unavailable")
        return [deepcopy(row) for row in self.documents.values() if matches(row, query or {})]

    def find_one(self, query, *args, **kwargs):
        return next(iter(self.find(query)), None)

    def update_one(self, query, update, *args, **kwargs):
        self.updates.append((deepcopy(query), deepcopy(update)))
        if self.before_update is not None:
            callback, self.before_update = self.before_update, None
            callback(self)
        if self.fail_next_update:
            self.fail_next_update = False
            raise RuntimeError("simulated_write_failed_before_commit")
        for document in self.documents.values():
            if not matches(document, query):
                continue
            old = deepcopy(document)
            for field, delta in update.get("$inc", {}).items():
                current = value_at(document, field)
                set_at(document, field, (0 if current is MISSING else current) + delta)
            for field, value in update.get("$set", {}).items():
                set_at(document, field, value)
            for field in update.get("$unset", {}):
                parts = field.split(".")
                target = document
                for part in parts[:-1]:
                    target = target.get(part, {})
                target.pop(parts[-1], None)
            if self.fail_after_commit:
                self.fail_after_commit = False
                raise RuntimeError("simulated_commit_succeeded_but_acknowledgement_lost")
            return SimpleNamespace(matched_count=1, modified_count=int(old != document))
        return SimpleNamespace(matched_count=0, modified_count=0)


def account(**changes):
    return {
        "_id": USER,
        "password": "test-password:with-colon",
        "blocked": False,
        "unlimited_user": False,
        "max_download_bytes": 0,
        "expiration_days": 0,
        "status": "On-hold",
        **changes,
    }


def successful_auth(collection, password="test-password:with-colon", now=NOW):
    return authorize(collection, USER + ":" + password, now=now)


class AuthenticationTests(unittest.TestCase):
    def test_zero_limits_allow_without_starting_first_use(self):
        collection = MemoryCollection(account())
        before = deepcopy(collection.documents)
        self.assertEqual(successful_auth(collection), {"ok": True, "id": USER})
        self.assertEqual(collection.documents, before)
        self.assertEqual(collection.updates, [])

    def test_password_can_contain_colon_and_must_match(self):
        collection = MemoryCollection(account())
        self.assertTrue(successful_auth(collection)["ok"])
        self.assertFalse(successful_auth(collection, "wrong")["ok"])
        self.assertFalse(authorize(collection, "other:test-password:with-colon", now=NOW)["ok"])

    def test_blocked_denies_even_unlimited_accounts(self):
        collection = MemoryCollection(account(blocked=True, unlimited_user=True))
        self.assertFalse(successful_auth(collection)["ok"])

    def test_unlimited_user_still_requires_correct_password(self):
        collection = MemoryCollection(account(unlimited_user=True,
            account_creation_date="2020-01-01", expiration_days=1,
            max_download_bytes=1, upload_bytes=1, download_bytes=100))
        self.assertTrue(successful_auth(collection)["ok"])
        self.assertFalse(successful_auth(collection, "wrong")["ok"])

    def test_absent_or_null_session_policy_preserves_legacy_response(self):
        for changes in ({}, {"max_sessions": None}):
            with self.subTest(changes=changes):
                collection = MemoryCollection(account(**changes))
                before = deepcopy(collection.documents)
                self.assertEqual(successful_auth(collection), {"ok": True, "id": USER})
                self.assertEqual(collection.documents, before)
                self.assertEqual(collection.updates, [])

    def test_session_policy_is_returned_without_activating_account(self):
        for limit in (1, 128):
            with self.subTest(limit=limit):
                collection = MemoryCollection(account(max_sessions=limit))
                before = deepcopy(collection.documents)
                self.assertEqual(successful_auth(collection),
                    {"ok": True, "id": USER, "max_sessions": limit})
                self.assertEqual(collection.documents, before)
                self.assertEqual(collection.updates, [])

    def test_unlimited_traffic_account_still_returns_session_policy(self):
        collection = MemoryCollection(account(unlimited_user=True, max_sessions=1,
            expiration_days=1, account_creation_date="2020-01-01",
            max_download_bytes=1, upload_bytes=1, download_bytes=100))
        self.assertEqual(successful_auth(collection),
            {"ok": True, "id": USER, "max_sessions": 1})
        self.assertFalse(successful_auth(collection, "wrong")["ok"])

    def test_malformed_session_policy_denies_even_unlimited_traffic_accounts(self):
        for value in (True, False, 0, -1, 129, "1", 1.0, [], {}):
            for unlimited in (False, True):
                with self.subTest(value=value, unlimited=unlimited):
                    collection = MemoryCollection(account(max_sessions=value, unlimited_user=unlimited))
                    before = deepcopy(collection.documents)
                    self.assertEqual(successful_auth(collection), {"ok": False})
                    self.assertEqual(collection.documents, before)
                    self.assertEqual(collection.updates, [])

    def test_pending_session_close_denies_auth_until_marker_is_cleared(self):
        for unlimited in (False, True):
            with self.subTest(unlimited=unlimited):
                collection = MemoryCollection(account(max_sessions=1, unlimited_user=unlimited,
                                                      _vpn_session_close_pending=True))
                before = deepcopy(collection.documents)
                self.assertEqual(successful_auth(collection), {"ok": False})
                self.assertEqual(collection.documents, before)
                self.assertEqual(collection.updates, [])
                collection.documents[USER].pop("_vpn_session_close_pending")
                self.assertEqual(successful_auth(collection),
                    {"ok": True, "id": USER, "max_sessions": 1})

    def test_quota_exactly_exhausted_denies_both_directions(self):
        collection = MemoryCollection(account(max_download_bytes=1024, upload_bytes=400, download_bytes=624))
        self.assertFalse(successful_auth(collection)["ok"])
        collection.documents[USER]["download_bytes"] = 623
        self.assertTrue(successful_auth(collection)["ok"])

    def test_day_expiry_uses_utc_midnight_boundary(self):
        collection = MemoryCollection(account(expiration_days=7, account_creation_date="2026-11-02"))
        before = dt.datetime(2026, 11, 8, 23, 59, 59, tzinfo=dt.timezone.utc)
        at = dt.datetime(2026, 11, 9, 0, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(successful_auth(collection, now=before)["ok"])
        self.assertFalse(successful_auth(collection, now=at)["ok"])

    def test_unactivated_limited_period_still_allows_without_mutation(self):
        collection = MemoryCollection(account(expiration_days=7))
        self.assertTrue(successful_auth(collection)["ok"])
        self.assertNotIn("account_creation_date", collection.documents[USER])
        self.assertEqual(collection.updates, [])

    def test_malformed_authorization_is_denied(self):
        collection = MemoryCollection(account())
        for presented in (None, 1, [], {}, "", "no-colon", ":password", USER + ":"):
            with self.subTest(presented=presented):
                self.assertFalse(authorize(collection, presented, now=NOW)["ok"])

    def test_malformed_unicode_is_a_clean_denial(self):
        collection = MemoryCollection(account())
        for presented in (USER + ":\ud800", "\udfff:password"):
            self.assertEqual(authorize(collection, presented, now=NOW), {"ok": False})
        self.assertEqual(collection.updates, [])

    def test_valid_unicode_password_is_compared_as_utf8(self):
        collection = MemoryCollection(account(password="пароль:🛡️"))
        self.assertTrue(successful_auth(collection, "пароль:🛡️")["ok"])
        self.assertFalse(successful_auth(collection, "пароль:🛡")["ok"])

    def test_malformed_policy_fails_closed(self):
        for changes in ({"max_download_bytes": "100"}, {"expiration_days": "7"},
                        {"upload_bytes": -1}, {"download_bytes": True},
                        {"expiration_days": 7, "account_creation_date": "invalid-date"}):
            with self.subTest(changes=changes):
                self.assertFalse(successful_auth(MemoryCollection(account(**changes)))["ok"])

    def test_database_failure_never_grants_access(self):
        collection = MemoryCollection(account())
        collection.fail_reads = True
        try:
            result = successful_auth(collection)
        except Exception:
            return  # The HTTP layer may translate unavailable to a non-2xx denial.
        self.assertFalse(result["ok"])


class AccountingTests(unittest.TestCase):
    def test_cumulative_snapshot_replay_and_increment_count_once(self):
        collection = MemoryCollection(account())
        first = {USER: {"tx": 100, "rx": 200}}
        apply_snapshot(collection, "epoch-one", first, {}, today=TODAY)
        apply_snapshot(collection, "epoch-one", first, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (100, 200))
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 150, "rx": 350}}, {}, today=TODAY)
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (150, 350))

    def test_new_server_epoch_adds_new_counters_without_resetting_totals(self):
        collection = MemoryCollection(account())
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 100, "rx": 200}}, {}, today=TODAY)
        apply_snapshot(collection, "epoch-two", {USER: {"tx": 3, "rx": 5}}, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (103, 205))
        self.assertEqual(row["_vpn_collector"]["epoch"], "epoch-two")

    def test_unexpected_clear_in_same_epoch_is_not_recounted(self):
        collection = MemoryCollection(account())
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 100, "rx": 200}}, {}, today=TODAY)
        before = deepcopy(collection.documents)
        with self.assertRaises(RuntimeStateError) as caught:
            apply_snapshot(collection, "epoch-one", {USER: {"tx": 2, "rx": 3}}, {}, today=TODAY)
        self.assertEqual(str(caught.exception), "collector_counter_regressed")
        self.assertEqual(collection.documents, before)

    def test_idle_first_use_is_activated_by_online_or_positive_traffic(self):
        for traffic, online in (({USER: {"tx": 1, "rx": 0}}, {}), ({}, {USER: 1})):
            with self.subTest(traffic=traffic, online=online):
                collection = MemoryCollection(account())
                apply_snapshot(collection, "epoch-one", traffic, online, today=TODAY)
                self.assertEqual(collection.documents[USER]["account_creation_date"], "2026-11-09")

    def test_idle_unactivated_user_stays_on_hold(self):
        collection = MemoryCollection(account())
        apply_snapshot(collection, "epoch-one", {}, {}, today=TODAY)
        self.assertNotIn("account_creation_date", collection.documents[USER])
        self.assertEqual(collection.documents[USER]["status"], "On-hold")

    def test_null_and_empty_activation_start_once_only_after_observed_activity(self):
        for created in (None, ""):
            with self.subTest(created=created):
                collection = MemoryCollection(account(account_creation_date=created))
                apply_snapshot(collection, "epoch-one", {}, {}, today=TODAY)
                self.assertEqual(collection.documents[USER]["account_creation_date"], created)
                self.assertEqual(collection.documents[USER]["status"], "On-hold")
                apply_snapshot(collection, "epoch-one", {USER: {"tx": 1, "rx": 0}}, {USER: 2}, today=TODAY)
                self.assertEqual(collection.documents[USER]["account_creation_date"], "2026-11-09")
                apply_snapshot(collection, "epoch-one", {USER: {"tx": 1, "rx": 0}}, {},
                               today=TODAY + dt.timedelta(days=1))
                self.assertEqual(collection.documents[USER]["account_creation_date"], "2026-11-09")
                self.assertEqual(collection.documents[USER]["upload_bytes"], 1)

    def test_concurrent_activation_date_is_not_overwritten_by_first_traffic(self):
        collection = MemoryCollection(account(account_creation_date=None))
        def activate(database):
            database.documents[USER]["account_creation_date"] = "2026-10-31"
        collection.before_update = activate
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 3, "rx": 5}}, {}, today=TODAY)
        self.assertEqual(collection.documents[USER]["account_creation_date"], "2026-10-31")
        self.assertEqual(collection.documents[USER]["upload_bytes"], 3)
        self.assertEqual(collection.documents[USER]["download_bytes"], 5)

    def test_nonempty_invalid_activation_is_not_rewritten_by_accounting(self):
        collection = MemoryCollection(account(expiration_days=7, account_creation_date="invalid-date"))
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 3, "rx": 5}}, {}, today=TODAY)
        self.assertEqual(collection.documents[USER]["account_creation_date"], "invalid-date")
        self.assertEqual(successful_auth(collection), {"ok": False})

    def test_account_policy_and_password_survive_accounting(self):
        original = account(blocked=True, max_download_bytes=10000, expiration_days=14, max_sessions=1,
                           _vpn_session_close_pending=True,
                           password="a-different-password", account_creation_date="2026-10-31")
        collection = MemoryCollection(original)
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 50, "rx": 70}}, {}, today=TODAY)
        row = collection.documents[USER]
        for field in ("blocked", "password", "max_download_bytes", "expiration_days", "max_sessions", "_vpn_session_close_pending", "account_creation_date", "unlimited_user"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(row["status"], "Offline")

    def test_atomic_increment_preserves_concurrent_existing_usage_update(self):
        collection = MemoryCollection(account(upload_bytes=1000, download_bytes=2000))
        def mutate(database):
            database.documents[USER]["upload_bytes"] += 11
            database.documents[USER]["blocked"] = True
            database.documents[USER]["max_download_bytes"] = 12345
        collection.before_update = mutate
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 3, "rx": 5}}, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (1014, 2005))
        self.assertTrue(row["blocked"])
        self.assertEqual(row["max_download_bytes"], 12345)

    def test_failed_database_write_can_be_retried_without_double_count(self):
        collection = MemoryCollection(account())
        collection.fail_next_update = True
        snapshot = {USER: {"tx": 9, "rx": 17}}
        try:
            apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        except Exception:
            pass
        apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (9, 17))

    def test_lost_write_acknowledgement_replays_persisted_cursor(self):
        collection = MemoryCollection(account())
        collection.fail_after_commit = True
        snapshot = {USER: {"tx": 9, "rx": 17}}
        with self.assertRaises(RuntimeStateError):
            apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        # The first call did commit, so retry must read its checkpoint.
        apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (9, 17))

    def test_concurrent_checkpoint_does_not_count_the_same_raw_bytes_twice(self):
        collection = MemoryCollection(account(upload_bytes=0, download_bytes=0,
            _vpn_collector={"epoch": "epoch-one", "tx": 0, "rx": 0, "seq": 1}))
        def another_collector(database):
            row = database.documents[USER]
            row["upload_bytes"], row["download_bytes"] = 10, 20
            row["_vpn_collector"] = {"epoch": "epoch-one", "tx": 10, "rx": 20, "seq": 2}
        collection.before_update = another_collector
        snapshot = {USER: {"tx": 10, "rx": 20}}
        try:
            apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        except RuntimeStateError:
            pass
        apply_snapshot(collection, "epoch-one", snapshot, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (10, 20))

    def test_unknown_stat_identity_does_not_create_accounts(self):
        collection = MemoryCollection(account())
        apply_snapshot(collection, "epoch-one", {"unknown-user": {"tx": 100, "rx": 200}}, {"unknown-user": 1}, today=TODAY)
        self.assertEqual(set(collection.documents), {USER})
        self.assertNotIn("account_creation_date", collection.documents[USER])

    def test_invalid_present_cursor_is_rejected_without_writes(self):
        for cursor in (None, [], "invalid"):
            with self.subTest(cursor=cursor):
                collection = MemoryCollection(account(_vpn_collector=cursor))
                before = deepcopy(collection.documents)
                with self.assertRaises(RuntimeStateError):
                    apply_snapshot(collection, "epoch-one", {USER: {"tx": 7, "rx": 11}}, {}, today=TODAY)
                self.assertEqual(collection.documents, before)
                self.assertEqual(collection.updates, [])

    def test_cursor_without_sequence_is_safely_initialized(self):
        collection = MemoryCollection(account(upload_bytes=10, download_bytes=20,
            _vpn_collector={"epoch": "epoch-one", "tx": 3, "rx": 5}))
        apply_snapshot(collection, "epoch-one", {USER: {"tx": 7, "rx": 11}}, {}, today=TODAY)
        row = collection.documents[USER]
        self.assertEqual((row["upload_bytes"], row["download_bytes"]), (14, 26))
        self.assertGreaterEqual(row["_vpn_collector"]["seq"], 1)


if __name__ == "__main__":
    unittest.main()
