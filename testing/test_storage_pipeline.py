"""Storage flow regression checks; no cloud credentials needed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from botocore.exceptions import ClientError
from storage import hf_sync, s3


class FakeS3:
    def __init__(self):
        self.items = {}
        self.uploads = 0

    def head_object(self, Bucket, Key):
        if Key not in self.items:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        data, meta = self.items[Key]
        return {"ContentLength": len(data), "ETag": '"testetag"', "Metadata": meta}

    def upload_file(self, filename, bucket, key, Config=None, ExtraArgs=None):
        self.uploads += 1
        self.items[key] = (Path(filename).read_bytes(), (ExtraArgs or {}).get("Metadata", {}))

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.items[key][0])

    def generate_presigned_url(self, method, Params, ExpiresIn):
        assert method == "get_object"
        assert ExpiresIn == 3600
        return "https://test.r2/" + Params["Key"]

    def get_paginator(self, name):
        return self

    def paginate(self, Bucket, Prefix):
        return [{"Contents": [{"Key": k, "Size": len(v[0])} for k, v in self.items.items()
                              if k.startswith(Prefix)]}]


class StorageTests(unittest.TestCase):
    def test_archive_three_categories_and_r2_only_images(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folders = {name: root / name for name in ("3d", "3dgs", "video")}
            for folder in folders.values():
                folder.mkdir()
            (folders["3d"] / "model.glb").write_bytes(b"glb")
            (folders["3dgs"] / "scene.ply").write_bytes(b"ply")
            (folders["video"] / "clip.mp4").write_bytes(b"video")
            (folders["video"] / "clip_preview.png").write_bytes(b"png")
            hf = FakeS3()
            observed, done = {}, {}
            with patch.object(hf_sync, "SOURCES", folders), \
                 patch.object(hf_sync, "PREVIEWS", root / "previews"), \
                 patch.object(hf_sync, "WORKFLOW", "test"), \
                 patch.object(hf_sync, "INTERVAL", 1), \
                 patch.object(hf_sync, "make_video_preview"):
                hf_sync.archive_once(hf, observed, done, now=0)
                self.assertEqual(hf.uploads, 0)
                hf_sync.archive_once(hf, observed, done, now=2)
                self.assertEqual(hf.uploads, 4)
                self.assertEqual(len(done), 4)
                self.assertEqual((root / "previews/video/clip_preview.png").read_bytes(), b"png")
                hf_sync.archive_once(hf, observed, done, now=3)
                self.assertEqual(hf.uploads, 4)
                self.assertIn("comfyui/test/3dgs/scene.ply", hf.items)
                self.assertIn("comfyui/test/video/clip.mp4", hf.items)

    def test_failed_upload_retries_without_false_success(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "video"
            folder.mkdir()
            (folder / "a.mp4").write_bytes(b"data")
            hf = FakeS3()
            class FailOnce(FakeS3):
                def upload_file(self, *args, **kwargs):
                    if not self.uploads:
                        self.uploads += 1
                        raise OSError("transient failure")
                    super().upload_file(*args, **kwargs)
            hf = FailOnce()
            seen, done = {}, {}
            with patch.object(hf_sync, "SOURCES", {"video": folder}), \
                 patch.object(hf_sync, "INTERVAL", 1), \
                 patch.object(hf_sync, "make_video_preview"):
                hf_sync.archive_once(hf, seen, done, now=0)
                hf_sync.archive_once(hf, seen, done, now=2)
                self.assertFalse(done)
                hf_sync.archive_once(hf, seen, done, now=4)
                self.assertEqual(len(done), 1)

    def test_download_reuses_r2_and_no_modal_dependency(self):
        hf, r2 = FakeS3(), FakeS3()
        key = "comfyui/004-test/video/clip.mp4"
        hf.items[key] = (b"video", {})
        def get_client(source):
            return (hf, "hf-bucket") if source == "hf" else (r2, "r2-bucket")
        with patch.object(s3, "client", side_effect=get_client):
            first = s3.download_via_r2(key)
            again = s3.download_via_r2(key)
        self.assertFalse(first["reused"])
        self.assertTrue(again["reused"])
        self.assertEqual(r2.uploads, 1)
        self.assertEqual(first["url"], "https://test.r2/downloads/" + key)

    def test_keys_and_asset_categories(self):
        hf, r2 = FakeS3(), FakeS3()
        hf.items["comfyui/foo/3dgs/a.ply"] = (b"model", {})
        hf.items["comfyui/foo/video/a.mp4"] = (b"movie", {})
        hf.items["comfyui/foo/other/hidden.mp4"] = (b"hidden", {})
        r2.items["previews/foo/video/a_preview.png"] = (b"png", {})
        with patch.object(s3, "client", side_effect=lambda src: (hf if src == "hf" else r2, "bucket")):
            self.assertEqual(len(s3.list_assets("hf", "foo")), 2)
            self.assertEqual(len(s3.list_assets("r2", "foo")), 1)
        for key in ("comfyui/foo/video/../../bad.mp4", "comfyui/foo/notvideo/anything.mp4", "comfyui/foo/video/a.exe"):
            with self.assertRaises(ValueError):
                s3.check_key(key, "hf")


if __name__ == "__main__":
    unittest.main()
