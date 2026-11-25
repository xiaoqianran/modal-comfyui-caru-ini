"""SDK reuse tests: retain fresh deployment checks and authoritative evidence."""
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_run as runner
from storage import delivery as d, execution as e, workflow as w
from storage.delivery_client import DeliveryClient
from storage.execution_client import DurableClient
from test_gc import InventoryVolume, fixture


class SDKReuseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"app": "test-app", "workflow": "test", "instance": 1,
                    "volume": "test-vol", "version": "a" * 64}
        self.base = "https://example.invalid"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vol = InventoryVolume(self.root)
        self.f = fixture(self.vol, self.cfg, self.root)

    def client(self, cls=DurableClient):
        with patch("modal.Volume.from_name", return_value=self.vol):
            return cls(self.cfg, self.base)

    def test_deployment_probe_reuse_checks_lifecycle_on_every_use(self):
        c = self.client(DeliveryClient)
        c.modal = Mock()
        name = "delivery_info_" + self.cfg["version"][:32]
        app = c.modal.App.lookup.return_value
        app.info.return_value = SimpleNamespace(app_id="ap", lifecycle=SimpleNamespace(version=1),
                                               functions={name: None})
        probe = c.modal.Function.from_name.return_value.remote
        probe.return_value = {**self.cfg, "delivery": "cpu", "instance_count": 1}
        model = c.modal.Cls.from_name.return_value.return_value
        model.ui.get_web_url.return_value = self.base
        for _ in range(4):
            self.assertEqual(c.preflight()["version"], self.cfg["version"])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(app.info.call_count, 5)
        model.ui.get_web_url.assert_called_once()
        app.info.return_value.lifecycle.version = 2
        c.preflight()
        self.assertEqual(probe.call_count, 2)
        app.info.return_value.functions = {}
        with self.assertRaisesRegex(RuntimeError, "not deployed"):
            c.preflight()
        self.assertIsNone(c._verified_model)

    def test_preflight_detects_change_during_probe_without_cache(self):
        c = self.client(DeliveryClient)
        c.modal = Mock()
        name = "delivery_info_" + self.cfg["version"][:32]
        c.modal.App.lookup.return_value.info.side_effect = [
            SimpleNamespace(app_id="ap", lifecycle=SimpleNamespace(version=i), functions={name: None})
            for i in (1, 2)]
        c.modal.Function.from_name.return_value.remote.return_value = {
            **self.cfg, "delivery": "cpu", "instance_count": 1}
        c.modal.Cls.from_name.return_value.return_value.ui.get_web_url.return_value = self.base
        with self.assertRaisesRegex(RuntimeError, "changed during preflight"):
            c.preflight()
        self.assertIsNone(c._probe_info)

    def test_request_body_reused_but_ledger_never_cached(self):
        c = self.client()
        req = self.f["shared"]
        ident = req["job_id"]
        key = e.request_key(self.cfg, ident)
        self.vol.reads.clear()
        self.assertEqual(c.status(ident)["status"], "prepared")
        self.assertEqual(c.status(ident)["status"], "prepared")
        e._write_event(self.vol, req, "claim", epoch="owner", owner="owner", prompt_id=ident)
        self.assertEqual(c.status(ident)["status"], "admitted")
        e._write_event(self.vol, req, "failed", epoch="owner", prompt_id=ident, error="failed")
        self.assertEqual(c.status(ident)["status"], "failed")
        reads = Counter(self.vol.reads)
        self.assertEqual(reads[key], 1)
        self.assertGreaterEqual(reads[e.event_key(self.cfg, ident, "failed")], 4)
        self.assertEqual(self.client().status(ident)["status"], "failed")
        self.assertEqual(self.vol.reads.count(key), 2)

    def test_staged_request_cached_immutably(self):
        c = self.client()
        req = self.f["shared"]
        ident = req["job_id"]
        c.remember_request(req)
        req["graph"]["2"]["inputs"]["filename_prefix"] = "mutated"
        self.vol.reads.clear()
        self.assertEqual(c.status(ident)["status"], "prepared")
        self.assertNotIn(e.request_key(self.cfg, ident), self.vol.reads)
        self.assertNotEqual(c._request(ident)["graph"]["2"]["inputs"]["filename_prefix"], "mutated")

    def test_request_cache_has_count_and_byte_bounds(self):
        c = self.client()
        req = self.f["shared"]
        for i in range(70):
            ident = e.job_id(["cache", i], req["intent"])
            c.remember_request({**req, "job_id": ident})
        self.assertEqual(len(c._requests), 64)
        for i in range(6):
            ident = e.job_id(["large", i], req["intent"])
            graph = json.loads(json.dumps(req["graph"]))
            graph["2"]["inputs"]["test_padding"] = "x" * (3 * 1024 * 1024)
            c.remember_request({**req, "job_id": ident, "graph": graph})
        self.assertLessEqual(c._request_bytes, 16 * 1024 * 1024)
        self.assertEqual(c._request_bytes, sum(map(len, c._requests.values())))

    def test_same_source_resolved_once_per_planned_request(self):
        plan = json.loads(json.dumps(self.f["plan"]))
        first = plan["jobs"][0]
        graph = json.loads(json.dumps(plan["graphs"][first["graph"]]))
        graph["3"] = {"class_type": "LoadImage", "inputs": {"image": None}}
        digest = e.sha_json(graph)
        plan["graphs"][digest] = graph
        second = {**first, "step": "consumer", "graph": digest, "inputs": {},
                  "sources": {field: {"job": ["future", "main"], "kind": "image"}
                              for field in ("1.image", "3.image")}}
        plan["jobs"].append(second)
        w.validate_plan(plan)
        identifier = w.plan_id(plan)
        item = {"filename": "result.png", "subfolder": "image/result", "type": "output"}
        def reference(volume, config, job_id, source):
            return {"sha256": "a" * 64, "bytes": 10, "extension": ".png", "source": source}
        with patch.object(e, "cloud_input_ref", side_effect=reference) as resolve:
            request = w.build_request(self.vol, plan, identifier, second,
                                      {("future", "main"): [item]})
        resolve.assert_called_once()
        self.assertEqual(request["inputs"]["1.image"], request["inputs"]["3.image"])

    def test_pending_call_handle_reused_and_terminal_handle_released(self):
        c = self.client()
        c.modal = Mock()
        c.modal.FunctionCall.from_id.return_value.get.side_effect = [TimeoutError(), TimeoutError(), {"job_id": self.f["upstream"]["job_id"]}]
        ident = self.f["upstream"]["job_id"]
        self.assertIsNone(c.poll(ident, "fc-test"))
        self.assertIsNone(c.poll(ident, "fc-test"))
        self.assertEqual(c.poll(ident, "fc-test")["status"], "completed")
        c.modal.FunctionCall.from_id.assert_called_once_with("fc-test")
        self.assertFalse(c._calls)

    def test_scope_reuses_exact_identity_only(self):
        job = {"delivery": self.cfg, "base": self.base}
        with patch("storage.execution_client.DurableClient") as factory:
            factory.side_effect = lambda *args: Mock()
            with runner._client_scope():
                a = runner._get_client(job, durable=True)
                self.assertIs(a, runner._get_client(job, durable=True))
                self.assertIsNot(a, runner._get_client({**job, "base": "https://other.invalid"}, durable=True))
                self.assertIsNot(a, runner._get_client({**job, "delivery": {**self.cfg, "version": "b" * 64}}, durable=True))
            self.assertIsNot(a, runner._get_client(job, durable=True))
            self.assertEqual(factory.call_count, 4)

    def test_collect_uses_in_memory_receipt_no_repeated_status_or_client(self):
        result = self.client().status(self.f["upstream"]["job_id"])
        folder = self.root / "output"
        job = {"execution": "durable", "collect": "cloud", "delivery": self.cfg, "base": self.base,
               "endpoint": "lab", "intent_hash": self.f["upstream"]["intent"], "folder": folder,
               "status_file": folder / "status.json", "durable_job_id": result["job_id"]}
        runner._apply_durable_result(("s", "j"), job, result)
        with patch("storage.execution_client.DurableClient", side_effect=AssertionError("repeat status")), \
             patch("storage.delivery_client.DeliveryClient", side_effect=AssertionError("unnecessary download client")):
            outputs = runner._collect_job(("s", "j"), job, result["remote_files"])
        self.assertEqual(outputs.files, result["remote_files"])
        self.assertNotIn("_verified_completion", job)
        # A new process must not trust a saved status.json receipt.
        with patch("storage.execution_client.DurableClient") as factory:
            factory.return_value.status.return_value = result
            runner._collect_job(("s", "j"), job, result["remote_files"])
            factory.return_value.status.assert_called_once_with(result["job_id"])


if __name__ == "__main__":
    unittest.main()
