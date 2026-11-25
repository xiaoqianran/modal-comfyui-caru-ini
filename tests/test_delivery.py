"""Delivery receipts, verified streaming, CPU archive, and fail-closed runner tests."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import batch_run
from storage import delivery as d
from storage import delivery_archive as a
from storage.delivery_client import DeliveryClient


class MemoryVolume:
    def __init__(self):
        self.objects = {}
        self.reads = []
        self.commits = 0

    def read_file(self, key):
        self.reads.append(key)
        if key not in self.objects:
            raise FileNotFoundError(key)
        yield self.objects[key]

    def commit(self):
        self.commits += 1


class FakeHF:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail = False

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def upload_file(self, Filename, Bucket, Key):
        self.uploads.append(Key)
        if self.fail:
            raise OSError("injected upload failure")
        self.objects[Key] = Path(Filename).read_bytes()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.cfg = {"app": "test-app", "workflow": "test", "instance": 1,
                    "volume": "test-volume", "version": "a"*64}
        self.data = b"test-image"
        self.file = self.source / "test" / "folder" / "a.png"
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(self.data)
        self.item = {"filename": "a.png", "subfolder": "folder", "type": "output"}
        self.history = {"status": {"completed": True, "status_str": "success"},
                        "outputs": {"1": {"images": [self.item]}}}
        self.receipt = d.build_receipt(self.source, self.cfg, "prompt1", self.history,
                                       version=self.cfg["version"])
        self.ready = self.source / d.receipt_key("test", 1, "prompt1")
        self.v = MemoryVolume()

    def test_commit_data_before_receipt_and_idempotence(self):
        observations = []
        volume = SimpleNamespace(commit=lambda: observations.append(self.ready.exists()))
        self.assertEqual(d.publish_receipt(self.source, volume, self.receipt), self.receipt)
        self.assertEqual(observations, [False, True])
        d.publish_receipt(self.source, volume, self.receipt)
        self.assertEqual(observations, [False, True, True])

    def test_data_commit_failure_never_publishes(self):
        volume = Mock()
        volume.commit.side_effect = OSError("commit failed")
        with self.assertRaises(OSError):
            d.publish_receipt(self.source, volume, self.receipt)
        self.assertFalse(self.ready.exists())

    def test_existing_receipt_conflict_not_overwritten(self):
        d.publish_receipt(self.source, self.v, self.receipt)
        new = json.loads(json.dumps(self.receipt))
        new["files"][0]["sha256"] = "0"*64
        with self.assertRaisesRegex(RuntimeError, "Conflicting"):
            d.publish_receipt(self.source, self.v, new)
        self.assertEqual(json.loads(self.ready.read_text()), self.receipt)

    def test_require_success_and_reject_temp_traversal(self):
        for item in ({"filename":"../a.png"}, {"filename":"a.png","subfolder":"../x"},
                     {"filename":"a.png","type":"temp"}, {"filename":"a.part.png"},
                     {"filename":"a.png","subfolder":"__snapshot_warmup__"}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                d.output_ref(item)
        with self.assertRaises(RuntimeError):
            d.build_receipt(self.source, self.cfg, "bad", {"status":{"completed":False}})
        with self.assertRaisesRegex(RuntimeError, "version"):
            d.build_receipt(self.source, self.cfg, "bad", self.history, version="b"*64)

    def test_receipt_rejects_mismatched_workflow_version_and_key(self):
        for field, value in (("workflow","wrong"),("version","b"*64),("prompt_id","other")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                d.validate_receipt(dict(self.receipt, **{field:value}), self.cfg, "prompt1")
        new=json.loads(json.dumps(self.receipt))
        new["files"][0]["key"]="different/a.png"
        with self.assertRaises(ValueError):
            d.validate_receipt(new, self.cfg, "prompt1")

    def test_sha_mismatch_keeps_existing_local_file(self):
        target = self.root / "local.png"
        target.write_bytes(b"old")
        evidence = self.receipt["files"][0]
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            d.verified_copy([b"x"*len(self.data)], target, evidence)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertFalse(list(self.root.glob("*.part")))
        self.assertEqual(d.verified_copy([self.data], target, evidence)["sha256"], hashlib.sha256(self.data).hexdigest())

    def test_volume_ready_recovers_without_gpu(self):
        self.v.objects[d.receipt_key("test",1,"prompt1")] = json.dumps(self.receipt).encode()
        self.v.objects[self.receipt["files"][0]["key"]] = self.data
        client = object.__new__(DeliveryClient)
        client.config = self.cfg
        client.volume = self.v
        client.preflight = Mock(side_effect=AssertionError("must not wake GPU"))
        client._model = Mock(side_effect=AssertionError("must not resolve GPU"))
        ready = client.receipt("prompt1")
        target = self.root / "local.png"
        client.download(ready,self.item,target)
        self.assertEqual(target.read_bytes(),self.data)
        client.preflight.assert_not_called()
        client._model.assert_not_called()

    def test_archiver_retry_and_idempotent_completion(self):
        d.publish_receipt(self.source,self.v,self.receipt)
        hf=FakeHF();hf.fail=True
        result=a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=0,expected_volume="test-volume")
        self.assertEqual(result,{"processed":1,"completed":0,"failed":1})
        hf.fail=False
        self.assertEqual(a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=1)["processed"],0)
        result=a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=100)
        self.assertEqual(result,{"processed":1,"completed":1,"failed":0})
        key=d.archive_key(self.receipt,self.receipt["files"][0])
        self.assertEqual(hf.objects[key],self.data)
        self.assertEqual(a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=200)["processed"],0)
        self.assertEqual(len(hf.uploads),2)

    def test_archive_refuses_same_size_corruption(self):
        d.publish_receipt(self.source,self.v,self.receipt)
        self.file.write_bytes(b"x"*len(self.data))
        hf=FakeHF()
        result=a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=0)
        self.assertEqual(result["failed"],1)
        self.assertFalse(hf.uploads)

    def test_archive_does_not_skip_unverified_same_size_hf_object(self):
        d.publish_receipt(self.source,self.v,self.receipt)
        hf=FakeHF()
        key=d.archive_key(self.receipt,self.receipt["files"][0])
        hf.objects[key]=b"x"*len(self.data)
        self.assertEqual(a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=0)["completed"],1)
        self.assertEqual(hf.objects[key],self.data)
        self.assertEqual(hf.uploads,[key])

    def test_corrupt_archive_ledger_does_not_block_other_prompt(self):
        d.publish_receipt(self.source,self.v,self.receipt)
        bad=self.state/"archive"/"test"/"1"/"prompt1.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("corrupted")
        second=d.build_receipt(self.source,self.cfg,"prompt2",self.history,
                               version=self.cfg["version"])
        d.publish_receipt(self.source,self.v,second)
        hf=FakeHF()
        result=a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=0)
        self.assertEqual(result,{"processed":2,"completed":1,"failed":1})
        self.assertEqual(bad.read_text(),"corrupted")
        self.assertEqual(hf.objects[d.archive_key(second,second["files"][0])],self.data)

    def test_preview_retry_does_not_repeat_upload(self):
        d.publish_receipt(self.source,self.v,self.receipt)
        hf=FakeHF()
        with patch.object(a,"_preview",side_effect=[OSError("preview unavailable"),None]):
            self.assertEqual(a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=0)["failed"],1)
            self.assertEqual(a.archive_pending(self.source,self.state,self.v,hf,"bucket",now=100)["completed"],1)
        self.assertEqual(len(hf.uploads),1)

    def test_preflight_rejects_wrong_version_without_gpu(self):
        client = object.__new__(DeliveryClient)
        client.config = self.cfg
        client.base = "https://correct.invalid"
        client.modal = Mock()
        client.modal.App.lookup.return_value.info.return_value = SimpleNamespace(
            app_id="ap-test", lifecycle=SimpleNamespace(version=1), functions={"other":None})
        client._model = Mock(side_effect=AssertionError("must not resolve GPU"))
        with self.assertRaisesRegex(RuntimeError,"not deployed"):
            client.preflight()
        client.modal.Function.from_name.assert_not_called()
        client._model.assert_not_called()

    def test_preflight_rejects_wrong_native_endpoint(self):
        client = object.__new__(DeliveryClient)
        client.config = self.cfg
        client.base = "https://wrong.invalid"
        client.modal = Mock()
        client.modal.App.lookup.return_value.info.return_value = SimpleNamespace(
            app_id="ap-test", lifecycle=SimpleNamespace(version=1),
            functions={"delivery_info_"+self.cfg["version"][:32]:None})
        client.modal.Function.from_name.return_value.remote.return_value = {
            **self.cfg, "delivery":"cpu", "instance_count":1}
        client._model = Mock(return_value=SimpleNamespace(ui=SimpleNamespace(
            get_web_url=lambda: "https://correct.invalid")))
        with self.assertRaisesRegex(RuntimeError,"URL does not match"):
            client.preflight()

    def test_runner_collect_uses_volume_not_native_view(self):
        folder=self.root/"job";folder.mkdir()
        job={"base":"https://example.invalid","endpoint":"e","folder":folder,
             "status_file":folder/"status.json","prompt_id":"prompt1","intent_hash":"intent",
             "delivery":self.cfg}
        client=Mock()
        client.receipt.return_value=self.receipt
        client.download.side_effect=lambda receipt,item,target,deadline: d.verified_copy([self.data],target,self.receipt["files"][0])
        with patch("storage.delivery_client.DeliveryClient",return_value=client),patch.object(batch_run,"download",side_effect=AssertionError("native view must not be called")):
            files=batch_run._collect_job(("s","j"),job,[self.item])
        self.assertEqual(files[0].read_bytes(),self.data)
        self.assertEqual(json.loads(job["status_file"].read_text())["delivery_receipt"],self.receipt)


if __name__ == "__main__":
    unittest.main()
