"""HF S3 integration contract: user metadata is not persisted by HF gateway."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import hf_sync


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.uploads = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise hf_sync.ClientError({"Error": {"Code": "404", "Message": "not found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key]), "Metadata": {}}

    def upload_file(self, Filename, Bucket, Key, Config=None):
        self.uploads += 1
        self.objects[Key] = Path(Filename).read_bytes()


class HFSyncTests(unittest.TestCase):
    def test_size_match_without_metadata(self):
        hf = FakeBucket()
        hf.objects["k"] = b"sample"
        self.assertTrue(hf_sync.same_content(hf, "bucket", "k", 6, "digest"))
        self.assertFalse(hf_sync.same_content(hf, "bucket", "k", 5, "digest"))
        self.assertFalse(hf_sync.same_content(hf, "bucket", "absent", 6, "digest"))

    def test_uploaded_once_when_head_omits_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "video").mkdir()
            output = root / "video" / "probe.mp4"
            output.write_bytes(b"small synthetic mp4 test bytes")
            hf = FakeBucket()
            with patch.object(hf_sync, "ROOT", root), patch.object(hf_sync, "PREVIEWS", root / "previews"), patch.object(hf_sync, "INTERVAL", 1), patch.object(hf_sync, "make_video_preview"):
                observed, completed = {}, {}
                hf_sync.archive_once(hf, observed, completed, now=0)
                hf_sync.archive_once(hf, observed, completed, now=2)
                hf_sync.archive_once(hf, observed, completed, now=4)
                self.assertEqual(hf.uploads, 1)
                self.assertEqual(len(completed), 1)

    def test_existing_same_size_object_is_not_reuploaded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "video").mkdir()
            output = root / "video" / "probe.mp4"
            output.write_bytes(b"sample")
            hf = FakeBucket()
            hf.objects[f"comfyui/{hf_sync.WORKFLOW}/video/probe.mp4"] = b"sample"
            with patch.object(hf_sync, "ROOT", root), patch.object(hf_sync, "INTERVAL", 1), patch.object(hf_sync, "make_video_preview"):
                observed, completed = {}, {}
                hf_sync.archive_once(hf, observed, completed, now=0)
                hf_sync.archive_once(hf, observed, completed, now=2)
                self.assertEqual(hf.uploads, 0)
                self.assertEqual(len(completed), 1)


if __name__ == "__main__":
    unittest.main()
