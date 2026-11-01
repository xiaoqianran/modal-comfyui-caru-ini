"""Runner regression tests: no remote ComfyUI required."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_run


class RunnerTimeoutTests(unittest.TestCase):
    def test_queued_prompt_does_not_time_out(self):
        job = {"prompt_id": "queued", "base": "https://example.invalid"}
        with patch.object(batch_run, "get_json", return_value={}):
            self.assertFalse(batch_run._check_job(("video", "a"), job, {}))

    def test_remote_error_is_still_reported(self):
        job = {"prompt_id": "bad", "base": "https://example.invalid"}
        history = {"bad": {"status": {"status_str": "error", "messages": ["OOM"]}}}
        with patch.object(batch_run, "get_json", return_value=history):
            with self.assertRaisesRegex(RuntimeError, "remote error"):
                batch_run._check_job(("video", "a"), job, {})

    def test_negative_batch_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            task = root / "task.json"
            task.write_text(json.dumps({"name": "test", "steps": [{"id": "video"}]}), encoding="utf8")
            endpoints = root / "endpoints.json"
            endpoints.write_text("{}", encoding="utf8")
            with self.assertRaisesRegex(ValueError, "timeout"):
                batch_run._run_task_unlocked(task, endpoints, timeout=-1)


if __name__ == "__main__":
    unittest.main()
