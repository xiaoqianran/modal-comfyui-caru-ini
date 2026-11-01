"""Offline checks for Modal snapshot warmup (no GPU or network)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from snapshot_warmup import warmup_h3


ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "testing" / "snapshot_h3_int8_warmup_api.json"


class SnapshotWarmupTests(unittest.TestCase):
    def test_warmup_graph_is_tiny_int8_and_has_output(self):
        graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        self.assertEqual(graph["140:127"]["inputs"]["unet_name"], "minimax_h3_fl2va_int8_convrot.safetensors")
        self.assertEqual(graph["140:119"]["inputs"]["vae_name"], "minimax_h3_video_vae_int8_convrot.safetensors")
        self.assertEqual(graph["140:133"]["inputs"]["value"], 0.2)
        self.assertEqual(graph["140:143"]["inputs"]["sigmas"], "1.0, 0.0")
        self.assertEqual(graph["140:131"]["inputs"]["first_frame"], ["148", 0])
        self.assertEqual(graph["148"]["class_type"], "LoadImage")
        self.assertEqual(graph["148"]["inputs"]["image"], "001.png")
        self.assertTrue(graph["92"]["inputs"]["filename_prefix"].startswith("__snapshot_warmup__/"))

    def test_success_polls_to_completion_and_cleans_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "__snapshot_warmup__"
            out.mkdir()
            (out / "sample.mp4").write_bytes(b"test")
            seen = []

            def fake_request(base, path, payload=None):
                seen.append(path)
                if path == "/prompt":
                    self.assertEqual(payload["prompt"]["140:133"]["inputs"]["value"], 0.2)
                    return {"prompt_id": "test123", "node_errors": {}}
                if path == "/history/test123":
                    return {"test123": {"status": {"status_str": "success", "completed": True},
                                         "outputs": {"92": {"video": "ok"}}}}
                if path == "/queue":
                    return {"queue_running": [], "queue_pending": []}
                self.fail(f"Unexpected path: {path}")

            with patch("snapshot_warmup._json_request", side_effect=fake_request):
                warmup_h3(8000, str(GRAPH_PATH), tmp, timeout=10)
            self.assertEqual(seen, ["/prompt", "/history/test123", "/queue"])
            self.assertFalse(out.exists())

    def test_failed_warmup_requests_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen = []

            def fake_request(base, path, payload=None):
                seen.append(path)
                if path == "/prompt":
                    return {"prompt_id": "test123", "node_errors": {}}
                if path == "/history/test123":
                    return {"test123": {"status": {"status_str": "error",
                                                   "completed": False}, "outputs": {}}}
                if path == "/interrupt":
                    return {}
                self.fail(f"Unexpected path: {path}")

            with patch("snapshot_warmup._json_request", side_effect=fake_request):
                with self.assertRaises(RuntimeError):
                    warmup_h3(8000, str(GRAPH_PATH), tmp, timeout=10)
            self.assertIn("/interrupt", seen)


if __name__ == "__main__":
    unittest.main()
