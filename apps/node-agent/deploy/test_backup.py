"""Operational checks for WAL capture, archive boundaries and restore staging."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("control_backup", Path(__file__).with_name("backup.py"))
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "live.sqlite3"
        self.db = sqlite3.connect(self.db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA wal_autocheckpoint=0")
        self.db.execute("CREATE TABLE accesses (id TEXT PRIMARY KEY, service TEXT)")
        self.db.commit()
        self.db.execute("INSERT INTO accesses VALUES ('a', 'hysteria')")
        self.db.commit()
        self.tokens = self.root / "tokens.json"
        self.tokens.write_text(json.dumps({"tokens": [{"id": "operator", "sha256": "a" * 64,
                                                       "scopes": ["access:read"]}]}))
        self.deploy = self.root / "deploy.json"
        self.deploy.write_text('{"schema":"test"}')
        self.node = self.root / "node.env"
        self.node.write_text('VPN_CONTROL_NODE_ID=nl-test\nVPN_CONTROL_PUBLIC_HOST=example.invalid\n')
        self.peer = self.root / "hysteria-peer.crt"
        self.peer.write_text('-----BEGIN CERTIFICATE-----\nPUBLIC TEST CERTIFICATE\n-----END CERTIFICATE-----\n')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def create(self):
        return Path(B.create(self.db_path, self.tokens, self.deploy, self.root / "backups", self.node, self.peer)["archive"])

    def test_live_wal_backup_captures_committed_rows_and_private_extract(self):
        self.assertTrue(Path(str(self.db_path) + "-wal").exists())
        archive = self.create()
        target = self.root / "restoration-staging"
        result = B.verify(archive, target)
        self.assertTrue(result["verified"])
        with sqlite3.connect(target / "control.sqlite3") as copied:
            self.assertEqual(copied.execute("SELECT * FROM accesses").fetchall(), [("a", "hysteria")])
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertFalse(result["backend_accounts_included"])
        self.assertEqual((target / "node.env").read_bytes(), self.node.read_bytes())
        self.assertEqual((target / "hysteria-peer.crt").read_bytes(), self.peer.read_bytes())
        without_node = Path(B.create(self.db_path, self.tokens, self.deploy, self.root / "backups",
                                     self.root / "missing-node.env", self.root / "missing-peer.crt")["archive"])
        without_optional = B.verify(without_node)["members"]
        self.assertNotIn("node.env", without_optional)
        self.assertNotIn("hysteria-peer.crt", without_optional)

    def test_tampering_and_path_traversal_are_rejected(self):
        original = self.create()
        altered = self.root / "tampered.tar.gz"
        with tarfile.open(original, "r:gz") as old, tarfile.open(altered, "w:gz") as new:
            for entry in old:
                data = old.extractfile(entry).read()
                if entry.name == "tokens.json":
                    data = data.replace(b"operator", b"operatoX")
                new.addfile(entry, io.BytesIO(data))
        with self.assertRaisesRegex(B.BackupError, "member_hash_mismatch"):
            B.verify(altered)
        traversal = self.root / "traversal.tar.gz"
        with tarfile.open(traversal, "w:gz") as tar:
            entry = tarfile.TarInfo("../escaped")
            entry.size = 1
            tar.addfile(entry, io.BytesIO(b"x"))
        with self.assertRaisesRegex(B.BackupError, "unexpected_archive_member"):
            B.verify(traversal)
        self.assertFalse((self.root.parent / "escaped").exists())
        corrupt = self.root / "bad-gzip-crc.tar.gz"
        data = bytearray(original.read_bytes())
        data[-8] ^= 1
        corrupt.write_bytes(data)
        with self.assertRaisesRegex(B.BackupError, "invalid_archive"):
            B.verify(corrupt)

    def test_existing_extract_directory_is_never_overwritten(self):
        archive = self.create()
        target = self.root / "existing"
        target.mkdir()
        marker = target / "keep"
        marker.write_bytes(b"keep")
        with self.assertRaisesRegex(B.BackupError, "destination_exists"):
            B.verify(archive, target)
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertEqual(list(target.iterdir()), [marker])


if __name__ == "__main__":
    unittest.main()
