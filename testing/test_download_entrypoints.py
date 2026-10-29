"""Validate downloader defaults and delegated entrypoint without GPU/network."""
import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_download_default_shard_size_is_16gb(self):
        source = (ROOT / "download-models.py").read_text(encoding="utf-8")
        self.assertIn('"MODEL_DOWNLOAD_SHARD_TARGET_GB", 16, 1, 1024', source)
        self.assertIn('MAX_SHARDS = 16', source)
        self.assertIn('"HF_XET_HIGH_PERFORMANCE": "0"', source)
        self.assertIn('cpu=4.0, memory=4096', source)
        self.assertIn('"MODEL_DOWNLOAD_WORKERS", 2, 1, 8', source)

    def test_comfy_entrypoint_invokes_canonical_downloader(self):
        source = ROOT / "comfyui.py"
        module = ast.parse(source.read_text(encoding="utf-8"))
        fn = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                  and node.name == "download_models")
        fn.decorator_list = []
        namespace = {"root_dir": ROOT, "os": os}
        import subprocess
        namespace["subprocess"] = subprocess
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), namespace)
        with patch.object(subprocess, "run") as call:
            namespace["download_models"]()
        call.assert_called_once()
        args, kwargs = call.call_args
        self.assertEqual(args[0], ["uv", "run", "modal", "run", "download-models.py"])
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertTrue(kwargs["check"])


if __name__ == "__main__":
    unittest.main()
