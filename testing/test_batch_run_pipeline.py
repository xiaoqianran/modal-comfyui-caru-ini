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
            status_path = root / "outputs" / "trial" / "scenes" / "01" / "status.json"
            legacy = json.loads(status_path.read_text())
            legacy.pop("intent_hash")
            status_path.write_text(json.dumps(legacy))
            try:
                batch_run.run_task(root / "task.json", root / "endpoints.json")
                raise AssertionError("Legacy result must require explicit trust")
            except RuntimeError as exc:
                assert "legacy status" in str(exc)
            batch_run.run_task(root / "task.json", root / "endpoints.json",
                               trust_legacy_status=True)
            assert "intent_hash" in json.loads(status_path.read_text())
            assert len(submitted) == 4
            task["steps"][0]["foreach"][0]["set"] = {"1.prompt": "changed prompt"}
            (root / "task.json").write_text(json.dumps(task))
            try:
                batch_run.run_task(root / "task.json", root / "endpoints.json")
                raise AssertionError("Changed prompt must not reuse existing result")
            except RuntimeError as exc:
                assert "changed" in str(exc)
            assert len(submitted) == 4, "Config mismatch must not submit GPU jobs"


def test_local_file_fingerprint():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        picture = root / "input.png"
        picture.write_bytes(b"image one")
        job = {
            "base": "https://q.example", "graph": {"1": {"inputs": {}}},
            "settings": {"1.image": "@input.png"}, "entry": {"name": "01"},
            "deps": set(),
        }
        one = batch_run._job_intent_hash(job, {}, root, root)
        picture.write_bytes(b"image two")
        two = batch_run._job_intent_hash(job, {}, root, root)
        assert one != two, "Changing local image content must invalidate result"


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


def test_preflight_and_model_reference():
    graph = {"1": {"inputs": {"image": "", "prompt": ""}},
             "2": {"inputs": {"image": ["1", 0]}}}
    batch_run._validate_graph(graph, {"1.prompt": "text"}, ["2"])
    for settings, outputs in [({"9.prompt": "x"}, None),
                              ({"1.nonexistent": "x"}, None),
                              ({}, ["99"])]:
        try:
            batch_run._validate_graph(graph, settings, outputs)
            raise AssertionError("Expected invalid graph to fail preflight")
        except ValueError:
            pass
    try:
        batch_run._validate_graph({"1": {"inputs": {"image": ["99", 0]}}}, {})
        raise AssertionError("Broken node link should fail")
    except ValueError:
        pass
    with tempfile.TemporaryDirectory() as tmp:
        model = Path(tmp) / "mesh.glb"
        model.write_bytes(b"glb")
        result = batch_run._input_value("$mesh.01.model", {}, {"mesh": {"01": [model]}})
        assert result == "@" + str(model.resolve())
        try:
            batch_run.upload("https://example.com", model)
            raise AssertionError("Non-image upload must not use image endpoint")
        except ValueError as exc:
            assert "upload" in str(exc)


if __name__ == "__main__":
    test_job_level_pipeline("stream")
    test_job_level_pipeline("batch")
    test_lock_and_upload_safety()
    test_local_file_fingerprint()
    test_preflight_and_model_reference()
    print("PASS stream/batch scheduling, cross-endpoint upload, local download, resume")
