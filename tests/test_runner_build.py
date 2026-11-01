"""Regression tests for Runner job construction and dependency handling."""
import json
import tempfile
import unittest
from pathlib import Path

from batch_run import _build_jobs


class RunnerBuildJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.graph = self.root / "graph.json"
        self.graph.write_text(json.dumps({
            "1": {"class_type": "LoadImage", "inputs": {"image": "default.png"}},
            "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}
        }), encoding="utf-8")
        self.endpoints = {"pika": "https://example.invalid/"}

    def test_foreach_jobs_and_dependency(self):
        steps = [
            {"id": "image", "endpoint": "pika", "workflow": "graph.json",
             "foreach": [{"name": "a"}, {"name": "b"}]},
            {"id": "video", "endpoint": "pika", "workflow": "graph.json",
             "foreach": [{"name": "clip", "set": {"1.image": "$image.a.image"}}]},
        ]
        jobs, ids = _build_jobs(steps, self.endpoints, self.root, self.root / "out", self.root)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(ids, {"image", "video"})
        self.assertEqual(jobs[("video", "clip")]["deps"], {("image", "a")})
        self.assertEqual(jobs[("video", "clip")]["base"], "https://example.invalid")
        self.assertNotEqual(jobs[("image", "a")]["intent_hash"], jobs[("video", "clip")]["intent_hash"])

    def test_invalid_future_dependency_is_rejected(self):
        steps = [{"id": "video", "endpoint": "pika", "workflow": "graph.json",
                  "set": {"1.image": "$image.main.image"}}]
        with self.assertRaisesRegex(ValueError, "earlier job"):
            _build_jobs(steps, self.endpoints, self.root, self.root / "out", self.root)

    def test_invalid_node_override_is_rejected(self):
        steps = [{"id": "video", "endpoint": "pika", "workflow": "graph.json",
                  "set": {"99.prompt": "unused"}}]
        with self.assertRaisesRegex(ValueError, "Invalid task target"):
            _build_jobs(steps, self.endpoints, self.root, self.root / "out", self.root)


if __name__ == "__main__":
    unittest.main()
