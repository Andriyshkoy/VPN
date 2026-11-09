import json
from pathlib import Path
import tempfile
import unittest

from vpn_control.common import BackendUnavailable
from vpn_control.hys_bridge import connect_mongo, load_mongo_configuration


class MongoConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "mongo.json"
        self.value = {
            "uri": "mongodb://runtime:private-secret@127.0.0.1:27018/fresh_db?authSource=admin&authMechanism=SCRAM-SHA-256",
            "database": "fresh_db", "collection": "accesses",
        }

    def tearDown(self):
        self.directory.cleanup()

    def write(self, value=None, mode=0o600):
        self.path.write_text(json.dumps(self.value if value is None else value))
        self.path.chmod(mode)

    def test_legacy_defaults_are_unchanged_without_file(self):
        self.assertEqual(load_mongo_configuration(), {
            "uri": "mongodb://127.0.0.1:27017/", "database": "blitz_panel", "collection": "users",
        })

    def test_custom_private_credentials_choose_actual_database_and_collection(self):
        self.write(mode=0o400)
        selected = []
        collection = object()

        class Database:
            def __getitem__(self, name):
                selected.append(name)
                return collection

        class Client:
            def __init__(self, uri, **options):
                self.uri, self.options = uri, options

            def __getitem__(self, name):
                selected.append(name)
                return Database()

        client, result = connect_mongo(self.path, client_factory=Client)
        self.assertIs(result, collection)
        self.assertEqual(client.uri, self.value["uri"])
        self.assertEqual(selected, ["fresh_db", "accesses"])
        self.assertEqual(client.options, {
            "serverSelectionTimeoutMS": 3000, "connectTimeoutMS": 3000, "socketTimeoutMS": 3000,
        })

    def test_loopback_ipv6_is_supported(self):
        value = {**self.value, "uri": "mongodb://runtime:private-secret@[::1]:27017/fresh_db?authSource=fresh_db"}
        self.write(value)
        self.assertEqual(load_mongo_configuration(self.path), value)

    def test_missing_insecure_symlink_and_invalid_files_have_safe_errors(self):
        def invalid():
            with self.assertRaisesRegex(BackendUnavailable, "^hysteria_mongo_credentials_invalid$") as caught:
                load_mongo_configuration(self.path)
            self.assertNotIn("private-secret", str(caught.exception))

        invalid()
        for mode in (0o644, 0o640, 0o666, 0o700):
            self.write(mode=mode)
            invalid()
        self.path.unlink()
        target = Path(self.directory.name) / "target.json"
        target.write_text(json.dumps(self.value)); target.chmod(0o600)
        self.path.symlink_to(target)
        invalid()
        self.path.unlink()
        self.path.write_text('{"uri":"private-secret"'); self.path.chmod(0o600)
        invalid()

    def test_remote_multiple_hosts_and_dangerous_options_are_rejected(self):
        uris = [
            "mongodb+srv://runtime:private-secret@localhost/fresh_db",
            "mongodb://runtime:private-secret@database.invalid:27017/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.2:27017/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017,localhost:27017/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:0/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:65536/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017/other_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db#ignored",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?tlsAllowInvalidCertificates=true",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?authSource=other_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?authMechanism=PLAIN",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?authSource=admin&authSource=fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?authSource",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db?authSource=",
            "mongodb://runtime@127.0.0.1:27017/fresh_db",
            "mongodb://:private-secret@127.0.0.1:27017/fresh_db",
            "mongodb://runtime:@127.0.0.1:27017/fresh_db",
            "mongodb://runtime:%GG@127.0.0.1:27017/fresh_db",
            "mongodb://runtime:private-secret@127.0.0.1:27017/fresh_db\n",
        ]
        for uri in uris:
            with self.subTest(uri=uri):
                self.write({**self.value, "uri": uri})
                with self.assertRaisesRegex(BackendUnavailable, "^hysteria_mongo_credentials_invalid$") as caught:
                    load_mongo_configuration(self.path)
                self.assertNotIn("private-secret", str(caught.exception))

    def test_unknown_keys_or_invalid_identifiers_are_rejected(self):
        values = [
            {**self.value, "extra": "private-secret"},
            {k: v for k, v in self.value.items() if k != "database"},
            {**self.value, "database": "../admin"},
            {**self.value, "collection": "$cmd"},
            {**self.value, "database": ["fresh_db"]},
            {**self.value, "uri": None},
        ]
        for value in values:
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaisesRegex(BackendUnavailable, "^hysteria_mongo_credentials_invalid$"):
                    load_mongo_configuration(self.path)

    def test_driver_failure_does_not_expose_credentials(self):
        self.write()

        def failing(uri, **options):
            raise ValueError("driver rejected " + uri)

        with self.assertRaisesRegex(BackendUnavailable, "^hysteria_mongo_connection_unavailable$") as caught:
            connect_mongo(self.path, client_factory=failing)
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertNotIn("mongodb://", str(caught.exception))

    def test_client_is_closed_if_collection_selection_fails(self):
        self.write()
        clients = []

        class Client:
            def __init__(self, uri, **options):
                self.closed = False
                clients.append(self)

            def __getitem__(self, name):
                raise ValueError("private-secret")

            def close(self):
                self.closed = True

        with self.assertRaisesRegex(BackendUnavailable, "^hysteria_mongo_connection_unavailable$"):
            connect_mongo(self.path, client_factory=Client)
        self.assertTrue(clients[0].closed)


if __name__ == "__main__":
    unittest.main()
