import unittest

from hysteria_runtime.runtime import authorize, nonnegative_int


class BSONIntegerTests(unittest.TestCase):
    def test_large_stored_quota_accepts_bson_integer_subclasses(self):
        class StoredInt64(int):
            pass

        class Collection:
            def find_one(self, query):
                return {"_id": "sample", "password": "sample-pass", "max_download_bytes": StoredInt64(4 * 1024**3),
                        "upload_bytes": StoredInt64(1024**3), "download_bytes": StoredInt64(2 * 1024**3)}

        self.assertEqual(authorize(Collection(), "sample:sample-pass"), {"ok": True, "id": "sample"})
        self.assertEqual(nonnegative_int(StoredInt64(4 * 1024**3)), 4 * 1024**3)
        with self.assertRaises(Exception):
            nonnegative_int(True)


class RealBSONIntegerTests(unittest.TestCase):
    def test_real_bson_large_quota_auth_and_collector_cursor_replay(self):
        try:
            from bson import BSON
            from bson.int64 import Int64
        except ImportError:
            self.skipTest('Install requirements.lock for the real Mongo driver regression')
        from test_runtime import MemoryCollection, account, USER
        from hysteria_runtime.runtime import apply_snapshot
        quota = 10 * 1024**3
        raw = BSON.encode(account(max_download_bytes=quota, upload_bytes=3 * 1024**3,
            download_bytes=4 * 1024**3,
            _vpn_collector={'epoch': 'first', 'tx': 3 * 1024**3, 'rx': 4 * 1024**3, 'seq': Int64(2**33)}))
        row = BSON(raw).decode()
        self.assertIsInstance(row['max_download_bytes'], Int64)
        collection = MemoryCollection(row)
        self.assertTrue(authorize(collection, USER + ':test-password:with-colon')['ok'])
        traffic = {USER: {'tx': 3 * 1024**3 + 4096, 'rx': 4 * 1024**3 + 8192}}
        apply_snapshot(collection, 'first', traffic, {})
        expected = (3 * 1024**3 + 4096, 4 * 1024**3 + 8192)
        actual = collection.documents[USER]
        self.assertEqual((actual['upload_bytes'], actual['download_bytes']), expected)
        # Re-decode the persisted fields through the actual BSON codec, then retry.
        collection = MemoryCollection(BSON(BSON.encode(actual)).decode())
        apply_snapshot(collection, 'first', traffic, {})
        actual = collection.documents[USER]
        self.assertEqual((actual['upload_bytes'], actual['download_bytes']), expected)
        collection.documents[USER]['max_download_bytes'] = Int64(sum(expected))
        self.assertFalse(authorize(collection, USER + ':test-password:with-colon')['ok'])


if __name__ == "__main__": unittest.main()
