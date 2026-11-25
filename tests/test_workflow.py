"""Workflow plans: immutable identity, all-input staging and independent recovery."""
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import batch_run as runner
import workflow_run as wr
from storage import execution as e, workflow as w, delivery as d
from test_execution import Engine, MemoryVolume
from storage.execution_client import DurableClient as RealDurableClient


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "original"
        self.inputs.mkdir()
        self.output = self.root / "volume"
        self.output.mkdir()
        self.vol = MemoryVolume(self.output)
        self.cfg = {"app": "test-app", "workflow": "test", "instance": 1,
                    "volume": "test-vol", "version": "a" * 64}
        self.graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "C:/old/placeholder.png"}},
                      "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0],
                                                                  "filename_prefix": "image/result"}}}
        self.second = {**self.graph, "3": {"class_type": "LoadImage",
                                          "inputs": {"image": "old-future.png"}}}
        self.first_file = self.inputs / "first.png"
        self.late_file = self.inputs / "later.png"
        self.first_file.write_bytes(b"initial")
        self.late_file.write_bytes(b"only needed by second step")
        self.task = self.inputs / "task.json"
        self.endpoints = self.inputs / "endpoints.json"
        self.endpoints.write_text(json.dumps({"lab": "https://example.invalid"}))
        (self.inputs / "g.json").write_text(json.dumps(self.graph))
        (self.inputs / "second.json").write_text(json.dumps(self.second))
        common = {"endpoint": "lab", "execution": "durable", "delivery": self.cfg,
                  "output_nodes": ["2"]}
        self.steps = [{**common, "id": "first", "workflow": "g.json", "collect": "cloud",
                       "set": {"1.image": "@first.png"}},
                      {**common, "id": "second", "workflow": "second.json",
                       "set": {"1.image": "$first.image", "3.image": "$item.media"},
                       "foreach": [{"name": "main", "media": "@later.png"}]}]
        self.write_task()
        self.dispatches = []
        self.observations = []
        self.downloads = []
        self.preflights = []
        self.lock = threading.Lock()
        test = self

        class Client:
            def __init__(self, config, base):
                self.config, self.base, self.volume = config, base, test.vol

            def preflight(self):
                test.preflights.append(self.config)
                return {"execution_schema": 1, "execution_media_inputs": 1,
                        "execution_cloud_inputs": 1}

            def status(self, ident):
                req = e.load_request(self.volume, self.config, ident)
                state = e.inspect_execution(self.volume, req)
                if state["status"] == "produced":
                    return e.finalize_produced(self.volume, req, state)
                return state

            def start(self, ident):
                state = self.status(ident)
                if state["status"] in ("completed", "failed"):
                    return state
                if state["status"] in ("admitted", "submitted"):
                    test.observations.append(ident)
                    return {**state, "status": "observing", "ledger_status": state["status"],
                            "observer_call_status": "not_attached"}
                req = e.load_request(self.volume, self.config, ident)
                test.dispatches.append(ident)
                return e.execute(self.volume, req, Engine(test.output, test.vol, req),
                                 ident, test.root / "materialized" / ident, test.output, test.lock)

            def poll(self, ident, call_id=None):
                if call_id is not None:
                    raise AssertionError("unexpected call poll")
                return None

            def download(self, receipt, item, dest, check_deadline=lambda: None):
                test.downloads.append(item)
                ref = d.receipt_file(receipt, item)
                return d.verified_copy(self.volume.read_file(ref["key"]), dest, ref, check_deadline)

        self.client = Client
        for target in ("storage.execution_client.DurableClient", "storage.delivery_client.DeliveryClient"):
            patcher = patch(target, Client)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_task(self, mode="stream"):
        self.task.write_text(json.dumps({"name": "workflow-test", "mode": mode, "steps": self.steps}))

    def prepare(self):
        locator = wr.prepare(self.task, self.endpoints, volume=self.vol)
        ident = locator["workflow_id"]
        return ident, w.load_plan(self.vol, self.cfg["volume"], ident)

    def execute_first(self, ident, plan):
        req = w.build_request(self.vol, plan, ident, plan["jobs"][0], {})
        w.persist_request(self.vol, req)
        result = self.client(self.cfg, "https://example.invalid").start(req["job_id"])
        self.assertEqual(result["status"], "completed")
        return result

    def test_prepare_persists_future_input_without_dispatch_and_is_path_independent(self):
        ident, plan = self.prepare()
        self.assertEqual(len(self.preflights), 1)
        self.assertFalse(self.dispatches)
        self.assertEqual([j["status"] for j in wr.status(ident, "test-vol", volume=self.vol)["jobs"]],
                         ["not_prepared", "not_prepared"])
        for spec in plan["jobs"]:
            for ref in spec["inputs"].values():
                self.assertIn(e.input_key(ref), self.vol.objects)
        self.assertNotIn("C:/old/placeholder.png", json.dumps(plan))
        self.assertNotIn("later.png", json.dumps(plan))
        self.assertEqual(self.prepare()[0], ident)
        self.first_file.rename(self.inputs / "renamed.png")
        self.steps[0]["set"]["1.image"] = "@renamed.png"
        self.write_task()
        self.assertEqual(self.prepare()[0], ident)
        self.write_task(mode="batch")
        self.assertNotEqual(self.prepare()[0], ident)

    def test_complete_and_unstarted_jobs_recover_with_every_local_source_deleted(self):
        ident, plan = self.prepare()
        first = self.execute_first(ident, plan)
        shutil.rmtree(self.inputs)
        out = self.root / "new-machine"
        with patch.object(runner, "request", side_effect=AssertionError("native HTTP")), \
             patch.object(runner, "upload", side_effect=AssertionError("local upload")), \
             patch.object(e, "stage_input", side_effect=AssertionError("local staging")):
            wr.resume(ident, "test-vol", out, timeout=10, volume=self.vol)
            second = plan["jobs"][1]
            _, second_id = w.job_identity(ident, second)
            req = e.load_request(self.vol, self.cfg, second_id)
            self.assertEqual(req["inputs"]["1.image"]["source"]["job_id"], first["job_id"])
            future = req["inputs"]["3.image"]
            self.assertEqual((self.root / "materialized" / second_id / e.input_name(future)).read_bytes(),
                             b"only needed by second step")
            self.assertEqual(self.dispatches, [first["job_id"], second_id])
            self.assertEqual(len(self.downloads), 1)
            final = out / "second/main/result.png"
            self.assertEqual(final.read_bytes(), b"result")
            self.assertEqual(list((out / "first/main").glob("*.png")), [])
            wr.resume(ident, "test-vol", out, timeout=10, volume=self.vol)
            self.assertEqual(len(self.downloads), 1)
            self.assertEqual(len(self.dispatches), 2)
            shutil.rmtree(out)
            wr.resume(ident, "test-vol", self.root / "third-machine", timeout=10, volume=self.vol)
            self.assertEqual(len(self.dispatches), 2)
            self.assertEqual(len(self.downloads), 2)
        self.assertEqual([j["status"] for j in wr.status(ident, "test-vol", volume=self.vol)["jobs"]],
                         ["completed", "completed"])

    def test_active_claim_observed_without_spawn_after_local_loss(self):
        ident, plan = self.prepare()
        req = w.build_request(self.vol, plan, ident, plan["jobs"][0], {})
        w.persist_request(self.vol, req)
        e._write_event(self.vol, req, "claim", epoch="owner", owner="worker", prompt_id=req["job_id"])
        e._write_event(self.vol, req, "submitted", epoch="owner", prompt_id=req["job_id"])
        shutil.rmtree(self.inputs)
        with patch.object(runner, "_DURABLE_OBSERVATION_SECONDS", 0):
            with self.assertRaises(TimeoutError):
                wr.resume(ident, "test-vol", self.root / "fresh", timeout=10, volume=self.vol)
        self.assertEqual(self.dispatches, [])
        self.assertEqual(self.observations, [req["job_id"]])
        self.assertEqual([j["status"] for j in wr.status(ident, "test-vol", volume=self.vol)["jobs"]],
                         ["submitted", "not_prepared"])

    def test_failed_source_staging_never_publishes_plan(self):
        stage = e.stage_input
        def fail_later(volume, path, **kwargs):
            if Path(path) == self.late_file:
                raise OSError("later upload failed")
            return stage(volume, path, **kwargs)
        with patch.object(e, "stage_input", side_effect=fail_later):
            with self.assertRaisesRegex(OSError, "later upload failed"):
                self.prepare()
        self.assertFalse(any("/workflows/" in key for key in self.vol.objects))
        self.assertFalse(self.dispatches)

    def test_changed_future_input_never_publishes_plan(self):
        stage = e.stage_input
        def mutate(volume, path, **kwargs):
            if Path(path) == self.late_file:
                self.late_file.write_bytes(b"changed")
            return stage(volume, path, **kwargs)
        with patch.object(e, "stage_input", side_effect=mutate):
            with self.assertRaisesRegex(RuntimeError, "Input changed"):
                self.prepare()
        self.assertFalse(any("/workflows/" in key for key in self.vol.objects))

    def test_invalid_native_implicit_or_cross_volume_plan_fails_before_upload(self):
        original = self.steps[1]
        for invalid, message in (
            ({**original, "execution": "native"}, "durable execution"),
            ({**original, "delivery": {**self.cfg, "volume": "another"}}, "same Volume"),
            ({**original, "set": {"1.image": "$first.image"}, "foreach": [{"name": "main"}]},
             "Untracked native media input"),
            ({**original, "set": {"1.image": "$first.video", "3.image": "@later.png"}},
             "Incompatible workflow media dependency"),
        ):
            self.steps[1] = invalid
            self.write_task()
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.prepare()
        self.assertFalse(self.vol.objects)
        self.assertFalse(self.preflights)

    def test_probe_capability_required_before_any_input_upload(self):
        with patch.object(self.client, "preflight", return_value={"execution_schema": 1}):
            with self.assertRaisesRegex(RuntimeError, "direct cloud outputs"):
                self.prepare()
        self.assertFalse(self.vol.objects)

    def test_tampered_plan_or_wrong_volume_rejected(self):
        ident, plan = self.prepare()
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            w.load_plan(self.vol, "wrong", ident)
        changed = {**plan, "mode": "batch"}
        self.vol.objects[w.plan_key(ident)] = e.canonical(changed)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            w.load_plan(self.vol, "test-vol", ident)

    def test_tampered_existing_request_never_dispatches(self):
        ident, plan = self.prepare()
        req = w.build_request(self.vol, plan, ident, plan["jobs"][0], {})
        req["graph"]["2"]["inputs"]["filename_prefix"] = "image/changed"
        e.put_once(self.vol, e.request_key(self.cfg, req["job_id"]), req)
        with self.assertRaisesRegex(RuntimeError, "Stored graph differs"):
            wr.status(ident, "test-vol", volume=self.vol)
        with self.assertRaisesRegex(RuntimeError, "Stored request differs"):
            wr.resume(ident, "test-vol", self.root / "fresh", timeout=10, volume=self.vol)
        self.assertFalse(self.dispatches)

    def test_status_is_read_only_and_resume_repairs_ready(self):
        ident, plan = self.prepare()
        result = self.execute_first(ident, plan)
        key = d.receipt_key(self.cfg["workflow"], 1, result["prompt_id"])
        self.vol.objects.pop(key)
        before = dict(self.vol.objects)
        status = wr.status(ident, "test-vol", volume=self.vol)
        self.assertEqual(status["jobs"][0]["status"], "produced")
        self.assertEqual(self.vol.objects, before)
        shutil.rmtree(self.inputs)
        wr.resume(ident, "test-vol", self.root / "fresh", timeout=10, volume=self.vol)
        self.assertIn(key, self.vol.objects)
        self.assertEqual(len(self.dispatches), 2)

    def test_real_client_waits_for_live_ready_marker_without_media_read(self):
        ident, plan = self.prepare()
        result = self.execute_first(ident, plan)
        client = object.__new__(RealDurableClient)
        client.volume, client.config = self.vol, self.cfg
        ready_key = d.receipt_key(self.cfg["workflow"], 1, result["prompt_id"])
        ready = self.vol.objects.pop(ready_key)
        original = self.vol.read_file
        def read(key):
            if not key.endswith(".json"):
                raise AssertionError("unnecessary media read during live publication")
            yield from original(key)
        def publish(_):
            self.vol.objects[ready_key] = ready
        with patch.object(self.vol, "read_file", side_effect=read), patch(
                "storage.execution_client.time.sleep", side_effect=publish) as pause:
            completed = client.status(result["job_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["receipt"], result["receipt"])
        pause.assert_called_once()

    def test_real_client_missing_ready_still_repairs_or_rejects_corrupt_media(self):
        ident, plan = self.prepare()
        result = self.execute_first(ident, plan)
        client = object.__new__(RealDurableClient)
        client.volume, client.config = self.vol, self.cfg
        ready_key = d.receipt_key(self.cfg["workflow"], 1, result["prompt_id"])
        self.vol.objects.pop(ready_key)
        key = result["receipt"]["files"][0]["key"]
        original = self.vol.objects[key]
        self.vol.objects[key] = b"broken"
        with patch("storage.execution_client._READY_VISIBILITY_GRACE_SECONDS", 0):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                client.status(result["job_id"])
            self.assertNotIn(ready_key, self.vol.objects)
            self.vol.objects[key] = original
            self.assertEqual(client.status(result["job_id"])["status"], "completed")
        self.assertIn(ready_key, self.vol.objects)

    def test_graph_deduplicated_and_case_collisions_rejected(self):
        self.steps = [dict(self.steps[0], foreach=[{"name": "one"}, {"name": "two"}])]
        self.write_task()
        ident, plan = self.prepare()
        self.assertEqual(len(plan["graphs"]), 1)
        self.assertEqual(len(plan["jobs"]), 2)
        self.assertNotEqual(w.job_identity(ident, plan["jobs"][0])[1],
                            w.job_identity(ident, plan["jobs"][1])[1])
        self.steps[0]["foreach"][1]["name"] = "ONE"
        self.write_task()
        with self.assertRaisesRegex(ValueError, "case-colliding"):
            wr.compile_plan(self.task, self.endpoints)

    def test_different_output_directory_identity_rejected(self):
        ident, plan = self.prepare()
        out = self.root / "old"
        out.mkdir()
        (out / "workflow-run.json").write_text(json.dumps({"workflow_id": "other", "volume": "test-vol"}))
        with self.assertRaisesRegex(RuntimeError, "belongs to another workflow"):
            wr.resume(ident, "test-vol", out, timeout=10, volume=self.vol)
        self.assertFalse(self.dispatches)


if __name__ == "__main__":
    unittest.main()
