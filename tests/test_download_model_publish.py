"""Regression tests for model cache publication; no Modal credentials required."""
import ast
import os
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "download-models.py"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
names = {"_safe_path", "_cached_path", "_size_valid", "_publish", "_stage_dir"}
functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
module = ast.Module(body=functions, type_ignores=[])


class PublishTest(unittest.TestCase):
    def test_publish_new_and_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctx = {"Path": Path, "os": os, "CACHE_DIR": str(root)}
            exec(compile(module, str(SOURCE), "exec"), ctx)
            item = {"kind": "hf", "repo_id": "org/model", "filename": "weights.safetensors",
                    "revision": "abc", "digest": "a" * 64, "size": 4}
            stage = root / ".download-shards" / "run"
            staged_blob = stage / "models--org--model" / "blobs" / item["digest"]
            staged_blob.parent.mkdir(parents=True)
            staged_blob.write_bytes(b"test")
            source = ctx["_cached_path"](stage, item)
            source.parent.mkdir(parents=True)
            source.symlink_to(os.path.relpath(staged_blob, source.parent))
            ctx["_publish"](source, root, item)
            public = root / "models--org--model" / "blobs" / item["digest"]
            self.assertEqual(public.read_bytes(), b"test")
            self.assertFalse(staged_blob.exists())
            ctx["_publish"](source, root, item)
            self.assertEqual(ctx["_cached_path"](root, item).read_bytes(), b"test")

    def test_shared_blob_is_not_moved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctx = {"Path": Path, "os": os, "CACHE_DIR": str(root)}
            exec(compile(module, str(SOURCE), "exec"), ctx)
            item = {"kind": "hf", "repo_id": "org/model", "filename": "weights.safetensors",
                    "revision": "new", "digest": "b" * 64, "size": 4}
            blob = root / "models--org--model" / "blobs" / item["digest"]
            blob.parent.mkdir(parents=True)
            blob.write_bytes(b"good")
            source = ctx["_cached_path"](root / ".download-shards" / "run", item)
            source.parent.mkdir(parents=True)
            source.symlink_to(os.path.relpath(blob, source.parent))
            ctx["_publish"](source, root, item)
            self.assertEqual(blob.read_bytes(), b"good")
            self.assertEqual(ctx["_cached_path"](root, item).read_bytes(), b"good")

    def test_external_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ctx = {"Path": Path, "os": os, "CACHE_DIR": str(root)}
            exec(compile(module, str(SOURCE), "exec"), ctx)
            item = {"kind": "external", "filename": "model.bin", "size": 4}
            source = root / ".download-shards" / "model.bin"
            source.parent.mkdir()
            source.write_bytes(b"data")
            ctx["_publish"](source, root, item)
            self.assertEqual((root / "model.bin").read_bytes(), b"data")
            ctx["_publish"](source, root, item)


if __name__ == "__main__":
    unittest.main()
