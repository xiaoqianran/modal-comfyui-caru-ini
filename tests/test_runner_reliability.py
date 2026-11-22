import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_run as runner


class RunnerReliabilityTests(unittest.TestCase):
    def test_batch_can_depend_on_earlier_job_in_same_step(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "g.json").write_text(json.dumps({"1": {"inputs": {"image": ""}}}))
            (root / "endpoints.json").write_text('{"e": "http://test"}')
            step = {"id": "s", "endpoint": "e", "workflow": "g.json", "foreach": [
                {"name": "a"}, {"name": "b", "set": {"1.image": "$s.a.image"}}]}
            task = root / "task.json"
            task.write_text(json.dumps({"name": "test", "mode": "batch", "steps": [step]}))
            submitted = []

            def submit(key, job, completed, *_):
                submitted.append(key)
                completed[key[0]][key[1]] = []
                job["state"] = "completed"

            with patch.object(runner, "_submit_job", side_effect=submit), patch.object(
                    runner.time, "sleep", side_effect=AssertionError("scheduler stalled")):
                runner.run_task(task, root / "endpoints.json")
            self.assertEqual(submitted, [("s", "a"), ("s", "b")])

    def test_completed_history_without_outputs_stops_polling(self):
        job = {"base": "http://test", "prompt_id": "p"}
        history = {"p": {"status": {"completed": True}, "outputs": {}}}
        with patch.object(runner, "get_json", return_value=history):
            with self.assertRaisesRegex(RuntimeError, "no downloadable output"):
                runner._check_job(("s", "j"), job, {})

    def test_selected_outputs_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "g.json").write_text(json.dumps({n: {"inputs": {}} for n in ("1", "2")}))
            steps = [{"id": "s", "endpoint": "e", "workflow": "g.json", "output_nodes": [1]}]
            jobs, _ = runner._build_jobs(steps, {"e": "http://test"}, root, root, root)
            job = jobs["s", "main"]
            first = job["intent_hash"]
            job["folder"].mkdir(parents=True)
            job["prompt_id"] = "p"
            history = {"p": {"outputs": {n: {"images": [{"filename": n + ".png"}]} for n in ("1", "2")}}}
            completed = {"s": {}}
            with patch.object(runner, "get_json", return_value=history), patch.object(runner, "download", side_effect=lambda b, i, p: p.write_bytes(b"image")):
                runner._check_job(("s", "main"), job, completed)
            self.assertEqual([p.name for p in completed["s"]["main"]], ["1.png"])
            steps[0]["output_nodes"] = [2]
            jobs, _ = runner._build_jobs(steps, {"e": "http://test"}, root, root, root)
            self.assertNotEqual(first, jobs["s", "main"]["intent_hash"])

    def test_transient_poll_yields_and_does_not_sleep(self):
        job = {"base": "http://test", "prompt_id": "p"}
        with patch.object(runner.urllib.request, "urlopen", side_effect=TimeoutError), patch.object(runner.time, "sleep") as sleep:
            self.assertFalse(runner._check_job(("s", "j"), job, {}))
            sleep.assert_not_called()

    def test_deadline_caps_request_and_propagates(self):
        token = runner._DEADLINE.set(15)
        try:
            with patch.object(runner.time, "monotonic", return_value=10), patch.object(runner.urllib.request, "urlopen", side_effect=TimeoutError) as opening:
                with self.assertRaises(runner.RequestFailed):
                    runner.request("http://test", retries=1)
                self.assertEqual(opening.call_args.kwargs["timeout"], 5)
            with patch.object(runner.time, "monotonic", return_value=16):
                with self.assertRaises(TimeoutError):
                    runner._check_job(("s", "j"), {"base": "http://test", "prompt_id": "p"}, {})
        finally:
            runner._DEADLINE.reset(token)

    def test_stream_download_retries_partial_response_atomically(self):
        class Response(io.BytesIO):
            headers = {"Content-Length": "6"}

            def read(self, size=-1):
                assert 0 < size <= 1024 * 1024
                return super().read(size)

        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "clip.mp4"
            dest.write_bytes(b"old")
            with patch.object(runner.urllib.request, "urlopen", side_effect=[Response(b"bad"), Response(b"abcdef")]), patch.object(runner.time, "sleep"):
                runner.download("http://test", {"filename": "clip.mp4"}, dest)
            self.assertEqual(dest.read_bytes(), b"abcdef")
            self.assertFalse(dest.with_suffix(".mp4.part").exists())
            with patch.object(runner.urllib.request, "urlopen", side_effect=TimeoutError), patch.object(runner.time, "sleep"):
                with self.assertRaises(runner.RequestFailed):
                    runner.download("http://test", {"filename": "clip.mp4"}, dest)
            self.assertEqual(dest.read_bytes(), b"abcdef")


if __name__ == "__main__":
    unittest.main()
