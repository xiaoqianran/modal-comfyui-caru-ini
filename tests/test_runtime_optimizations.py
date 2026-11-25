"""Regression coverage for concurrent runner I/O and runtime lifecycle policy."""
import ast
import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_run as runner
import runtime_policy as policy
from storage import hf_sync

ROOT = Path(__file__).resolve().parents[1]


class RunnerIOTests(unittest.TestCase):
    def task(self, root, two_endpoints=True):
        (root / "graph.json").write_text(json.dumps({"1": {"inputs": {"prompt": ""}}}))
        (root / "endpoints.json").write_text(json.dumps({"a": "http://a", "b": "http://b"}))
        (root / "task.json").write_text(json.dumps({
            "name": "parallel",
            "steps": [
                {"id": "one", "endpoint": "a", "workflow": "graph.json"},
                {"id": "two", "endpoint": "b" if two_endpoints else "a", "workflow": "graph.json"},
            ],
        }))
        return root / "task.json", root / "endpoints.json"

    def test_independent_endpoint_submissions_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, endpoints = self.task(Path(tmp))
            barrier = threading.Barrier(2)
            def submit(url, *args, **kwargs):
                self.assertTrue(url.endswith("/prompt"))
                barrier.wait(timeout=3)
                return json.dumps({"prompt_id": url.split("/")[2]}).encode()
            def history(base, path):
                pid = path.rsplit("/", 1)[-1]
                return {pid: {"outputs": {"n": {"images": [{"filename": pid+".png"}]}}}}
            def download(base, item, target):
                target.write_bytes(b"synthetic image")
                return runner._file_evidence(target)
            with patch.object(runner, "request", side_effect=submit), \
                 patch.object(runner, "get_json", side_effect=history), \
                 patch.object(runner, "download", side_effect=download):
                out = runner.run_task(task, endpoints, timeout=10)
            self.assertEqual(len(list(out.rglob("status.json"))), 2)

    def test_slow_download_does_not_block_other_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, endpoints = self.task(Path(tmp), two_endpoints=False)
            downloading = threading.Event()
            polled = threading.Event()
            count = 0
            def submit(url, *args, **kwargs):
                nonlocal count
                count += 1
                return json.dumps({"prompt_id": str(count)}).encode()
            def history(base, path):
                pid = path.rsplit("/", 1)[-1]
                if pid == "2":
                    self.assertTrue(downloading.wait(3), "second history must overlap first download")
                    polled.set()
                return {pid: {"outputs": {"n": {"images": [{"filename": pid+".png"}]}}}}
            def download(base, item, target):
                if item["filename"] == "1.png":
                    downloading.set()
                    self.assertTrue(polled.wait(3), "download must not block second history")
                target.write_bytes(b"synthetic image")
                return runner._file_evidence(target)
            with patch.object(runner, "request", side_effect=submit), \
                 patch.object(runner, "get_json", side_effect=history), \
                 patch.object(runner, "download", side_effect=download):
                runner.run_task(task, endpoints, timeout=10)
            self.assertEqual(count, 2)

    def test_resume_rejects_same_size_corruption_and_uses_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"base": "http://a", "endpoint": "a", "folder": root,
                   "status_file": root / "status.json", "prompt_id": "p",
                   "intent_hash": "test"}
            files = [{"filename": n, "subfolder": "", "type": "output"}
                     for n in ("a.png", "b.png")]
            def download(base, item, target):
                target.write_bytes(item["filename"].encode())
                return runner._file_evidence(target)
            with patch.object(runner, "download", side_effect=download) as fetch:
                runner._collect_job(("s", "j"), job, files)
                self.assertEqual(fetch.call_count, 2)
                (root / "a.png").write_bytes(b"x.png")  # unchanged length
                runner._collect_job(("s", "j"), job, files)
                self.assertEqual(fetch.call_count, 3)
                self.assertEqual(fetch.call_args.args[1]["filename"], "a.png")
            status = json.loads(job["status_file"].read_text())
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["artifacts"]["a.png"]["sha256"],
                             hashlib.sha256(b"a.png").hexdigest())

    def test_collect_partial_resume_only_fetches_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = {"base": "http://a", "endpoint": "a", "folder": root,
                   "status_file": root / "status.json", "prompt_id": "p",
                   "intent_hash": "test"}
            files = [{"filename": n, "subfolder": "", "type": "output"}
                     for n in ("a.png", "b.png")]
            def download(base, item, target):
                target.write_bytes(b"data")
                return runner._file_evidence(target)
            def fail_second(base, item, target):
                if item["filename"] == "b.png":
                    raise OSError("simulated connection loss")
                return download(base, item, target)
            with patch.object(runner, "download", side_effect=fail_second):
                with self.assertRaises(OSError):
                    runner._collect_job(("s", "j"), job, files)
            self.assertEqual(json.loads(job["status_file"].read_text())["status"], "collecting")
            with patch.object(runner, "download", side_effect=download) as fetch:
                runner._collect_job(("s", "j"), job, files)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.args[1]["filename"], "b.png")

    def test_ambiguous_submission_never_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, endpoints = self.task(root)
            # Use only one endpoint to ensure a failure does not start another job.
            data = json.loads(task.read_text())
            data["steps"] = data["steps"][:1]
            task.write_text(json.dumps(data))
            with patch.object(runner, "request", side_effect=runner.RequestFailed("lost response")) as post:
                with self.assertRaisesRegex(RuntimeError, "submission uncertain"):
                    runner.run_task(task, endpoints, timeout=10)
                self.assertEqual(post.call_count, 1)
                status = json.loads((root/"outputs"/"parallel"/"one"/"main"/"status.json").read_text())
                self.assertEqual(status["status"], "uncertain")
                self.assertTrue(status["client_id"])
                with self.assertRaisesRegex(RuntimeError, "prior state"):
                    runner.run_task(task, endpoints, timeout=10)
                self.assertEqual(post.call_count, 1)

    def test_deadline_propagated_into_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, endpoints = self.task(root)
            def submit(url, *args, **kwargs):
                self.assertIsNotNone(runner._DEADLINE.get())
                raise runner.RequestFailed("lost response")
            with patch.object(runner, "request", side_effect=submit):
                with self.assertRaisesRegex(RuntimeError, "submission uncertain"):
                    runner.run_task(task, endpoints, timeout=5)

    def test_output_names_cannot_overwrite_runner_metadata(self):
        root = Path(".")
        files = [{"filename": n} for n in ("status.json", "foo.png", "foo.png", "002_foo.png", "FOO.png")]
        targets = [p.name.casefold() for p, _ in runner._artifact_targets({"folder": root}, files)]
        self.assertEqual(len(set(targets)), len(targets))
        self.assertNotIn("status.json", targets)


class LifecycleTests(unittest.TestCase):
    def test_profile_and_warmup_are_explicit(self):
        self.assertEqual(policy.resolve_policy("other", {}, {}).scaledown_window, 1800)
        self.assertEqual(policy.resolve_policy("comfyui-idol", {}, {}).scaledown_window, 300)
        self.assertEqual(policy.resolve_policy("other", {}, {"COMFYUI_RUNTIME_PROFILE": "batch"}).scaledown_window, 60)
        self.assertFalse(policy.resolve_policy("other", {"runtime": {"snapshot": "off"}}, {}).gpu_snapshot)
        self.assertEqual(policy.resolve_policy("010-minimax-h3-b200plus-suite", {}, {}).warmup, "h3-int8")
        for settings in ({"warmup": "h3-int8"}, {"scaledown_window": 0}, {"snapshot": "cpu"}, {"typo": 1}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                policy.resolve_policy("other", {"runtime": settings}, {})

    def test_readiness_checks_http_and_exited_process(self):
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(policy.urllib.request, "urlopen", return_value=io.BytesIO(b'{"system":{},"devices":[]}')):
            policy.wait_for_server(8000, proc, 1)
        proc.poll.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "exited with code 7"):
            policy.wait_for_server(8000, proc, 1)

    def cleanup_function(self, volume, stopper):
        tree = ast.parse((ROOT/"comfyui.py").read_text(encoding="utf-8"))
        klass = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name=="ComfyUI")
        fn = next(n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name=="cleanup")
        fn.decorator_list = []
        namespace = {"time": time, "log_event": Mock(), "output_volume": volume,
                     "stop_process": stopper}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<cleanup>", "exec"), namespace)
        return namespace["cleanup"]

    def test_cleanup_stops_producers_before_final_commit(self):
        order = []
        volume = SimpleNamespace(commit=lambda: order.append("commit"))
        stopper = lambda proc, label: order.append(label)
        thread = Mock()
        thread.is_alive.return_value = False
        self.cleanup_function(volume, stopper)(SimpleNamespace(
            procs=[object()], sync_proc=object(), commit_stop=Mock(), commit_thread=thread))
        self.assertEqual(order, ["comfy-1", "hf-sync", "commit"])

    def test_cleanup_never_overlaps_commits(self):
        volume = Mock()
        stopper = Mock()
        thread = Mock()
        thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "background Volume commit"):
            self.cleanup_function(volume, stopper)(SimpleNamespace(
                procs=[object()], sync_proc=object(), commit_stop=Mock(), commit_thread=thread))
        self.assertEqual(stopper.call_count, 2)
        volume.commit.assert_not_called()

    def test_model_link_rejects_truncated_hf_weight_before_symlink(self):
        import sys
        import types
        tree = ast.parse((ROOT/"comfyui.py").read_text(encoding="utf-8"))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in (
            "link_cached_models", "resolve_model_dir")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            revision = "a"*40
            key = "org/model/weight.safetensors"
            cached = root / "cached.safetensors"
            cached.write_bytes(b"bad")
            for directory, record in (
                (".workflow-revisions", {key: revision}),
                (".workflow-hf-sizes", {key: {"revision": revision, "size": 5, "digest": "b"*64}}),
            ):
                path = root / directory / "test.json"
                path.parent.mkdir()
                path.write_text(json.dumps(record))
            hf = types.ModuleType("huggingface_hub")
            hf.hf_hub_download = Mock(return_value=str(cached))
            errors = types.ModuleType("huggingface_hub.errors")
            errors.LocalEntryNotFoundError = type("LocalEntryNotFoundError", (Exception,), {})
            ns = {"Path": Path, "json": json, "WORKFLOW": "test",
                  "MODEL_CACHE_DIR": str(root), "COMFY_MODELS_ROOT": root/"models",
                  "models": [{"repo_id": "org/model", "filename": "weight.safetensors",
                              "model_dir": "checkpoints"}], "models_ext": [],
                  "log_event": Mock()}
            exec(compile(ast.Module(body=funcs, type_ignores=[]), "<model-link>", "exec"), ns)
            with patch.dict(sys.modules, {"huggingface_hub": hf, "huggingface_hub.errors": errors}):
                with self.assertRaisesRegex(RuntimeError, "size/revision mismatch"):
                    ns["link_cached_models"]()
            self.assertFalse((root/"models").exists())

    def test_stop_process_reaps_real_local_child(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 start_new_session=True)
        try:
            policy.stop_process(child, "local-test", timeout=3)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)

    def test_archiver_excludes_warmup_and_partial_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for file in ("real.png", "__snapshot_warmup__/warm.mp4", "video/clip_preview.part.png"):
                path = root / file
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake")
            with patch.object(hf_sync, "ROOT", root):
                self.assertEqual(list(dict(hf_sync.iter_outputs())), ["image/real.png"])


if __name__ == "__main__":
    unittest.main()
