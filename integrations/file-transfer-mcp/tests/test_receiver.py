"""Smoke tests for the PC2 download, digest check and ZIP path safety."""
import hashlib
import importlib.util
import io
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
