import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from build_artifact import build


class ArtifactTests(unittest.TestCase):
    def test_artifact_is_deterministic_and_excludes_private_and_test_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("README.md", "requirements.txt", "requirements.lock"):
                (root / name).write_text(name)
            package = root / "hysteria_runtime"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "runtime.py").write_text("pass\n")
            (package / "private").mkdir()
            (package / "private" / "secret.py").write_text("SECRET")
            (root / ".env").write_text("SECRET")
            (root / "private").mkdir()
            (root / "private" / "key.pem").write_text("SECRET")
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text("SECRET")
            first = build(root)
            self.assertEqual(first, build(root))
            with tarfile.open(fileobj=io.BytesIO(first), mode="r:gz") as archive:
                names = archive.getnames()
                self.assertEqual(len(names), 5)
                self.assertEqual(set(names), {"app/README.md", "app/requirements.txt", "app/requirements.lock",
                                             "app/hysteria_runtime/__init__.py", "app/hysteria_runtime/runtime.py"})
                self.assertTrue(all(m.mtime == 0 and m.uid == 0 and m.mode == 0o644 for m in archive.getmembers()))
            self.assertNotIn(b"SECRET", gzip.decompress(first))

    def test_source_symlink_is_not_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "hysteria_runtime").mkdir()
            (root / "README.md").symlink_to("/etc/passwd")
            for name in ("requirements.txt", "requirements.lock"):
                (root / name).write_text("")
            with self.assertRaisesRegex(ValueError, "source_file_invalid"):
                build(root)


if __name__ == "__main__":
    unittest.main()
