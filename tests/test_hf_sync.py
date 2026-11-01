"""HF S3 integration contract: user metadata is not persisted by HF gateway."""
import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import hf_sync
from storage import s3
from storage.output_formats import AUDIO, IMAGES


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
    def test_common_formats_can_be_archived_and_downloaded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for extension in AUDIO | IMAGES:
                (root / ("output" + extension)).write_bytes(b"sample")
            (root / "output.mp4.part").write_bytes(b"unfinished")
            with patch.object(hf_sync, "ROOT", root):
                outputs = dict(hf_sync.iter_outputs())
            self.assertEqual(len(outputs), len(AUDIO | IMAGES))
            for relative, path in outputs.items():
                category = "audio" if path.suffix in AUDIO else "image"
                self.assertTrue(relative.startswith(category + "/"))
                for source, key in (("hf", "comfyui/test/" + relative),
                                    ("r2", "downloads/comfyui/test/" + relative)):
                    self.assertEqual(s3.check_key(key, source), key)

    def test_changes_during_hash_or_upload_are_retried(self):
        for phase in ("hash", "upload", "failed_upload"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                output = root / "probe.mp4"
                output.write_bytes(b"before")
                hf = FakeBucket()
                key = f"comfyui/{hf_sync.WORKFLOW}/video/probe.mp4"
                real_hash = hf_sync.sha256_file
                real_upload = hf.upload_file

                def change_output():
                    stamp = output.stat().st_mtime_ns
                    output.write_bytes(b"after!")
                    os.utime(output, ns=(stamp + 1_000_000, stamp + 1_000_000))

                def hashing(path):
                    result = real_hash(path)
                    change_output()
                    return result

                def uploading(*args, **kwargs):
                    real_upload(*args, **kwargs)
                    if phase == "failed_upload":
                        hf.objects[key] = b"broken"
                        raise OSError("connection lost after write")
                    change_output()

                with patch.object(hf_sync, "ROOT", root), patch.object(hf_sync, "INTERVAL", 1), patch.object(hf_sync, "make_video_preview"):
                    observed, completed = {}, {}
                    hf_sync.archive_once(hf, observed, completed, now=0)
                    with patch.object(hf_sync, "sha256_file", hashing if phase == "hash" else real_hash), patch.object(hf, "upload_file", uploading):
                        hf_sync.archive_once(hf, observed, completed, now=2)
                    self.assertFalse(completed)
                    if phase == "hash":
                        self.assertEqual(hf.uploads, 0)
                    hf_sync.archive_once(hf, observed, completed, now=4)
                    hf_sync.archive_once(hf, observed, completed, now=6)
                    self.assertEqual(hf.objects[key], output.read_bytes())
                    self.assertEqual(len(completed), 1)

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
