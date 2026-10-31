"""Smoke tests for the PC2 download, digest check and ZIP path safety."""
import hashlib
import importlib.util
import io
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "pc2_receive.py"
spec = importlib.util.spec_from_file_location("pc2_receive", MODULE)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class ReceiverTests(unittest.TestCase):
    def test_zip_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "link.zip"
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr(link, "../outside")
            with self.assertRaisesRegex(ValueError, "symlink"):
                receiver.extract_zip_safely(archive, root / "out")

    def test_zip_existing_link_cannot_escape_destination(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            dest = root / "out"
            outside = root / "outside"
            dest.mkdir()
            outside.mkdir()
            try:
                (dest / "link").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"Symlink creation unavailable: {exc}")
            archive = root / "link.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("link/escape.txt", b"unsafe")
            with self.assertRaisesRegex(ValueError, "escapes"):
                receiver.extract_zip_safely(archive, dest)
            self.assertFalse((outside / "escape.txt").exists())

    def test_zip_validates_all_paths_before_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("first.txt", b"must-not-write")
                zf.writestr("../escape.txt", b"unsafe")
            with self.assertRaises(ValueError):
                receiver.extract_zip_safely(archive, root / "out")
            self.assertFalse((root / "out" / "first.txt").exists())

    def test_zip_extraction_limit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "large.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("file.txt", b"12345")
            with patch.object(receiver, "MAX_EXTRACT_BYTES", 4), self.assertRaises(ValueError):
                receiver.extract_zip_safely(archive, root / "out")
            self.assertFalse((root / "out" / "file.txt").exists())

    def test_download_limit_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source.bin"
            source.write_bytes(b"12345")
            target = root / "target.bin"
            target.write_bytes(b"existing")
            args = ["receive", "--url", source.as_uri(), "--sha256", "0" * 64,
                    "--output", str(target)]
            with patch.object(sys, "argv", args), patch.object(receiver, "MAX_DOWNLOAD_BYTES", 4):
                with self.assertRaisesRegex(ValueError, "limit"):
                    receiver.main()
            self.assertEqual(target.read_bytes(), b"existing")
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["source.bin", "target.bin"])

    def test_download_sha256_extract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source.zip"
            with zipfile.ZipFile(source, "w") as zf:
                zf.writestr("01.png", b"frame-01")
                zf.writestr("02.png", b"frame-02")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            target = root / "received.zip"
            dest = root / "images"
            args = ["receive", "--url", source.as_uri(), "--sha256", digest,
                    "--output", str(target), "--extract-to", str(dest)]
            with patch.object(sys, "argv", args), patch("sys.stdout", new_callable=io.StringIO):
                receiver.main()
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual((dest / "01.png").read_bytes(), b"frame-01")
            self.assertEqual((dest / "02.png").read_bytes(), b"frame-02")

    def test_invalid_digest_does_not_leave_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "original.bin"
            source.write_bytes(b"not-the-hash")
            target = root / "download.bin"
            args = ["receive", "--url", source.as_uri(), "--sha256", "0" * 64,
                    "--output", str(target)]
            with patch.object(sys, "argv", args), self.assertRaises(ValueError):
                receiver.main()
            self.assertFalse(target.exists())

    def test_zip_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../escape.txt", b"do-not-write")
            with self.assertRaises(ValueError):
                receiver.extract_zip_safely(archive, root / "out")
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
