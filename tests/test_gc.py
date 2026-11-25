"""Dependency-aware GC: retention roots and conservative offline deletion."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from storage import delivery as d, execution as e, workflow as w, gc
from test_execution import MemoryVolume


def put_bytes(volume, key, data):
    with volume.batch_upload(force=False) as batch:
        batch.put_file(io.BytesIO(data), key)
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def fixture(volume, cfg, root):
    """Same fixture works with MemoryVolume and real Modal Volume V2."""
    graph = {"1": {"class_type": "LoadImage", "inputs": {"image": "placeholder.png"}},
             "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "image/result"}}}
    paths, refs = {}, {}
    for name in ("shared", "future", "orphan", "second"):
        path = root / (name + ".png")
        path.write_bytes(("fixture-" + name).encode())
        paths[name] = path
        refs[name] = e.stage_input(volume, path)
    # A future job has no request yet but its input is a permanent root.
    spec = {"step": "future", "job": "main", "endpoint": "lab", "base": "https://example.invalid",
            "delivery": cfg, "graph": e.sha_json(graph), "settings": {}, "inputs": {"1.image": refs["future"]},
            "sources": {}, "output_nodes": ["2"], "collect": "local"}
    plan = {"schema": 1, "name": "future-workflow", "volume": cfg["volume"], "mode": "stream",
            "graphs": {spec["graph"]: graph}, "jobs": [spec]}
    plan_id = w.publish_plan(volume, plan)
    intent = "b" * 64

    def stage(label, path=None, source=None):
        ident = e.job_id(["gc-fixture", label], intent)
        if source:
            req = e.stage_request(volume, cfg, ident, intent, graph, {"1.image": None},
                                  ["2"], output_inputs={"1.image": source})
        else:
            req = e.stage_request(volume, cfg, ident, intent, graph, {"1.image": "@" + str(path)}, ["2"])
        return req

    def output_key(ident, name="output.png"):
        item = {"filename": name, "subfolder": f"image/{e.OUTPUT_NAMESPACE}/{ident}", "type": "output"}
        return item, d.source_key(cfg["workflow"], cfg["instance"], False, d.output_ref(item))

    def produce(req, ready):
        ident = req["job_id"]
        item, key = output_key(ident)
        evidence = put_bytes(volume, key, ("output-" + ident).encode())
        file = {"filename": item["filename"], "subfolder": item["subfolder"], "key": key, **evidence}
        receipt = {"schema": 1, **cfg, "prompt_id": ident, "dual": False, "files": [file]}
        e._write_event(volume, req, "claim", epoch="lab", owner="lab", prompt_id=ident)
        e._write_event(volume, req, "submitted", epoch="lab", prompt_id=ident)
        e._write_event(volume, req, "produced", epoch="lab", prompt_id=ident,
                       receipt=receipt, remote_files=[item])
        if ready:
            e.put_once(volume, d.receipt_key(cfg["workflow"], cfg["instance"], ident), receipt)
        return key, item, receipt

    upstream = stage("upstream", paths["shared"])
    source_key, item, ready = produce(upstream, True)
    downstream = stage("consumer", source={"config": cfg, "job_id": upstream["job_id"], "item": item})
    shared = stage("another-user", paths["shared"])
    pending_ready = stage("pending-ready", paths["second"])
    pending_key, _, pending_receipt = produce(pending_ready, False)
    failed = stage("failed", paths["shared"])
    e._write_event(volume, failed, "claim", epoch="lab", owner="lab", prompt_id=failed["job_id"])
    e._write_event(volume, failed, "failed", epoch="lab", prompt_id=failed["job_id"], error="fixture")
    _, failed_key = output_key(failed["job_id"], "partial.png")
    put_bytes(volume, failed_key, b"failed partial output")
    # Unknown and published successful extra outputs are deliberately retained.
    unknown = e.job_id(["unknown"], intent)
    _, unknown_key = output_key(unknown)
    put_bytes(volume, unknown_key, b"unknown output")
    _, extra_key = output_key(upstream["job_id"], "extra.png")
    put_bytes(volume, extra_key, b"unlisted success output")
    unmanaged = cfg["workflow"] + "/image/native.png"
    put_bytes(volume, unmanaged, b"native legacy output")
    return {"plan_id": plan_id, "plan": plan, "refs": refs, "paths": paths, "upstream": upstream,
            "downstream": downstream, "shared": shared, "pending": pending_ready,
            "pending_receipt": pending_receipt, "pending_key": pending_key,
            "source_key": source_key, "ready": ready, "failed": failed,
            "failed_key": failed_key, "unknown_key": unknown_key, "extra_key": extra_key,
            "unmanaged": unmanaged, "orphan_key": e.input_key(refs["orphan"])}


class InventoryVolume(MemoryVolume):
    def __init__(self, root):
        super().__init__(root)
        self.object_id = "vo-test"
        self.mtimes = {}
        self.removed = []
        self.reads = []

    def iterdir(self, path, recursive=True):
        assert path == "/" and recursive
        for key, data in sorted(self.objects.items()):
            yield SimpleNamespace(path="/" + key, type=1, size=len(data),
                                  mtime=self.mtimes.get(key, 1_800_000_000))

    def read_file(self, key):
        self.reads.append(key)
        yield from super().read_file(key)

    def remove_file(self, key, recursive=False):
        assert not recursive
        self.removed.append(key)
        del self.objects[key]


class GCTests(unittest.TestCase):
    NOW = 1_900_000_000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vol = InventoryVolume(self.root)
        self.cfg = {"app": "test-app", "workflow": "test", "instance": 1,
                    "volume": "test-vol", "version": "a" * 64}
        self.f = fixture(self.vol, self.cfg, self.root)

    def scan(self, age=168):
        return gc.scan(self.vol, "test-vol", min_age_hours=age, now=self.NOW)

    def apply(self, plan, confirm=True, now=None):
        return gc.apply(self.vol, "test-vol", plan, confirm_quiescent=confirm,
                        now=self.NOW if now is None else now)

    def test_mark_all_roots_and_sweep_only_two_safe_files(self):
        before = dict(self.vol.objects)
        self.vol.reads.clear()
        plan = self.scan()
        expected = {self.f["orphan_key"], self.f["failed_key"]}
        self.assertEqual({i["key"] for i in plan["candidates"]}, expected)
        self.assertEqual(plan["roots"], {"plans": 1, "requests": 5, "ready_receipts": 1,
                                        "produced_receipts": 2})
        self.assertTrue(all(k.endswith(".json") for k in self.vol.reads))
        self.assertEqual(self.vol.objects, before)
        self.assertFalse(self.vol.removed)
        result = self.apply(plan)
        self.assertEqual(set(self.vol.removed), expected)
        self.assertEqual(result["deleted_bytes"], sum(len(before[k]) for k in expected))
        self.assertEqual(self.vol.objects, {k: v for k, v in before.items() if k not in expected})
        self.assertEqual(self.scan()["candidates"], [])

    def test_age_and_unknown_mtime_protect_candidates(self):
        self.vol.mtimes[self.f["orphan_key"]] = self.NOW - 60
        self.vol.mtimes[self.f["failed_key"]] = 0
        self.assertEqual(self.scan()["candidates"], [])
        self.assertEqual([x["key"] for x in self.scan(age=0)["candidates"]], [self.f["orphan_key"]])

    def test_apply_needs_quiescence_and_fresh_exact_plan(self):
        plan = self.scan()
        with self.assertRaisesRegex(RuntimeError, "exclusive offline"):
            self.apply(plan, confirm=False)
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.apply(plan, now=self.NOW + 3601)
        self.vol.object_id = "vo-recreated"
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.apply(plan)
        self.assertFalse(self.vol.removed)

    def test_new_reference_after_preview_refuses_old_plan(self):
        plan = self.scan()
        req = self.f["shared"]
        ident = e.job_id(["new"], req["intent"])
        e.stage_request(self.vol, self.cfg, ident, req["intent"], req["graph"],
                        {"1.image": "@" + str(self.f["paths"]["orphan"])}, ["2"])
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.apply(plan)
        self.assertFalse(self.vol.removed)
        self.assertNotIn(self.f["orphan_key"], [x["key"] for x in self.scan()["candidates"]])

    def test_unresolved_claim_blocks_even_if_candidates_exist(self):
        req = self.f["shared"]
        e._write_event(self.vol, req, "claim", epoch="active", owner="owner", prompt_id=req["job_id"])
        plan = self.scan()
        self.assertTrue(plan["unresolved_claims"])
        with self.assertRaisesRegex(RuntimeError, "Unresolved execution claims"):
            self.apply(plan)
        self.assertFalse(self.vol.removed)

    def test_tampered_candidate_list_cannot_delete_retained_file(self):
        plan = self.scan()
        plan["candidates"].append({"key": self.f["source_key"], "bytes": 1, "mtime": 1,
                                   "reason": "forged"})
        with self.assertRaisesRegex(RuntimeError, "changed since"):
            self.apply(plan)
        self.assertFalse(self.vol.removed)

    def test_unknown_execution_schema_or_missing_request_fails_closed(self):
        unknown = ".execution/v2/roots.json"
        put_bytes(self.vol, unknown, b"{}")
        with self.assertRaisesRegex(RuntimeError, "Unknown execution object"):
            self.scan()
        del self.vol.objects[unknown]
        req = self.f["failed"]
        del self.vol.objects[e.request_key(self.cfg, req["job_id"])]
        with self.assertRaisesRegex(RuntimeError, "without request"):
            self.scan()
        self.assertFalse(self.vol.removed)

    def test_missing_future_input_blocks_gc(self):
        del self.vol.objects[e.input_key(self.f["refs"]["future"])]
        with self.assertRaisesRegex(RuntimeError, "Missing or changed-size retained"):
            self.scan()
        self.assertFalse(self.vol.removed)

    def test_source_ready_missing_or_conflicting_blocks_gc(self):
        req = self.f["upstream"]
        key = d.receipt_key(self.cfg["workflow"], 1, req["job_id"])
        del self.vol.objects[key]
        with self.assertRaisesRegex(RuntimeError, "Cloud input source receipt"):
            self.scan()
        self.assertFalse(self.vol.removed)

    def test_produced_without_ready_is_retained_and_can_recover_after_gc(self):
        self.apply(self.scan())
        req = self.f["pending"]
        completed = e.finalize_produced(self.vol, req, e.inspect_execution(self.vol, req))
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["receipt"], self.f["pending_receipt"])

    def test_failed_output_with_legacy_receipt_is_retained(self):
        req = self.f["failed"]
        path = self.f["failed_key"]
        item = {"filename": Path(path).name, "subfolder": path.split("/", 1)[1].rsplit("/", 1)[0]}
        data = self.vol.objects[path]
        receipt = {"schema": 1, **self.cfg, "prompt_id": "legacy-published",
                   "dual": False, "files": [{**item, "key": path, "bytes": len(data),
                                            "sha256": hashlib.sha256(data).hexdigest()}]}
        e.put_once(self.vol, d.receipt_key(self.cfg["workflow"], 1, "legacy-published"), receipt)
        self.assertNotIn(path, [x["key"] for x in self.scan()["candidates"]])


if __name__ == "__main__":
    unittest.main()
