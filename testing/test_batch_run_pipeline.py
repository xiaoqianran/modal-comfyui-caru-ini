"""Offline dependency scheduler tests; no Modal requests or GPU costs."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import batch_run


def test_job_level_pipeline(mode="stream"):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "g.json").write_text(json.dumps({"1": {"inputs": {"image": "", "prompt": ""}}}))
        (root / "endpoints.json").write_text(json.dumps({"q": "https://q.example", "v": "https://v.example"}))
        task = {"name": "trial", "mode": mode, "steps": [
            {"id": "scenes", "endpoint": "q", "workflow": "g.json", "foreach": [
                {"name": "01"}, {"name": "02"}]},
            {"id": "videos", "endpoint": "v", "workflow": "g.json", "foreach": [
                {"name": "01", "set": {"1.image": "$scenes.01.image"}},
                {"name": "02", "set": {"1.image": "$scenes.02.image"}}]}]}
        (root / "task.json").write_text(json.dumps(task))
        submitted = []
        polls = {}
        finished_second_image_at_submission = []
        def fake_request(url, data=None, headers=None, timeout=300, retries=6):
            if url.endswith("/prompt"):
                graph = json.loads(data)["prompt"]
                base = url.split("/prompt")[0]
                pid = str(len(submitted) + 1)
                if base == "https://v.example":
                    finished_second_image_at_submission.append(polls.get("2", 0) >= 4)
                submitted.append((base, graph["1"]["inputs"].get("image", ""), pid))
                polls[pid] = 0
                return json.dumps({"prompt_id": pid}).encode()
            raise AssertionError(url)
        def fake_history(base, path):
            pid = path.split("/")[-1]
            polls[pid] += 1
            # The first image is done immediately; second image needs several polls.
            if pid == "2" and polls[pid] < 4:
                return {}
            return {pid: {"outputs": {"out": {"images": [{"filename": pid + ".png"}]}}}}
        def fake_download(base, item, dest):
            dest.write_bytes(b"mock image")
        def fake_upload(base, file):
            assert Path(file).exists()
            return Path(file).name
        with patch.object(batch_run, "request", fake_request), \
             patch.object(batch_run, "get_json", fake_history), \
             patch.object(batch_run, "download", fake_download), \
             patch.object(batch_run, "upload", fake_upload), \
             patch.object(batch_run.time, "sleep", lambda _: None):
            batch_run.run_task(root / "task.json", root / "endpoints.json")
            assert [x[0] for x in submitted] == [
                "https://q.example", "https://q.example",
                "https://v.example", "https://v.example"]
            assert submitted[2][1] == "1.png"
            assert submitted[3][1] == "2.png"
            assert polls["2"] >= 4
            assert finished_second_image_at_submission == ([False, True] if mode == "stream" else [True, True])
            assert len(list((root / "outputs" / "trial").rglob("status.json"))) == 4
            batch_run.run_task(root / "task.json", root / "endpoints.json")
            assert len(submitted) == 4, "Resume must not resubmit jobs"


def test_lock_and_upload_safety():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with batch_run._task_lock(root):
            try:
                with batch_run._task_lock(root):
                    raise AssertionError("Second lock should fail")
            except RuntimeError as exc:
                assert "already running" in str(exc)
        with batch_run._task_lock(root):
            pass  # Released correctly
        a, b = root / "a.png", root / "b.png"
        a.write_bytes(b"image one")
        b.write_bytes(b"image two")
        payloads = []
        def fake_request(url, data=None, headers=None, timeout=300, retries=6):
            payloads.append(data)
            return b'{"name": "ok.png", "subfolder": ""}'
        with patch.object(batch_run, "request", fake_request):
            batch_run.upload("https://v.example", a)
            batch_run.upload("https://v.example", b)
        assert b"overwrite" in payloads[0] and b"false" in payloads[0]
        assert b"runner_" in payloads[0] and b"runner_" in payloads[1]
        assert payloads[0] != payloads[1]


if __name__ == "__main__":
    test_job_level_pipeline("stream")
    test_job_level_pipeline("batch")
    test_lock_and_upload_safety()
    print("PASS stream/batch scheduling, cross-endpoint upload, local download, resume")
