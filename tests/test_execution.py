"""Durable execution invariants and fail-closed recovery."""
import hashlib
import io
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import batch_run as runner
from storage import execution as e
from storage import delivery as d


class MemoryVolume:
    def __init__(self, root):
        self.root = root
        self.objects = {}
        self.lock = threading.Lock()
        self.commits = 0
        self.fail_commit = False

    def read_file(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        yield self.objects[key]

    @contextmanager
    def batch_upload(self, force=False):
        pending = {}
        class Batch:
            def put_file(self, value, key):
                pending[key] = value.read() if hasattr(value, "read") else Path(value).read_bytes()
        yield Batch()
        with self.lock:
            if not force and any(key in self.objects for key in pending):
                raise FileExistsError("Already exists")
            self.objects.update(pending)

    def commit(self):
        self.commits += 1
        if self.fail_commit:
            raise OSError("injected commit failure")
        for path in self.root.rglob("*"):
            if path.is_file():
                self.objects[path.relative_to(self.root).as_posix()] = path.read_bytes()


class Engine:
    def __init__(self, root, volume, request, crash=False):
        self.root, self.volume, self.request = root, volume, request
        self.submits = 0
        self.record = None
        self.crash = crash

    def submit(self, graph, identifier):
        # The native POST must be preceded by an authoritative claim.
        assert e.inspect_execution(self.volume, self.request)["status"] == "admitted"
        self.submits += 1
        p = self.root / "test" / "result.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"result")
        self.record = {"status": {"status_str": "success", "completed": True},
                       "outputs": {"2": {"images": [{"filename": "result.png", "type": "output"}]}}}
        if self.crash:
            raise OSError("Lost native reply after acceptance")
        return identifier

    def history(self, identifier):
        return self.record

    def contains(self, identifier):
        return False


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "outputs"
        self.output.mkdir()
        self.vol = MemoryVolume(self.output)
        self.cfg = {"app":"test-app","workflow":"test","instance":1,"volume":"test-vol","version":"a"*64}
        self.graph = {"1":{"class_type":"LoadImage","inputs":{"image":"unused.png"}},
                      "2":{"class_type":"SaveImage","inputs":{"images":["1",0],"filename_prefix":"result"}}}
        self.source = self.root / "source.png"
        self.source.write_bytes(b"original-image")
        self.intent = "b"*64
        self.identifier = e.job_id(["task","step","main"],self.intent)
        self.req = e.stage_request(self.vol,self.cfg,self.identifier,self.intent,self.graph,
                                   {"1.image":"@"+str(self.source)},["2"])
        self.lock = threading.Lock()

    def run_engine(self, engine, epoch="epoch1"):
        return e.execute(self.vol,self.req,engine,epoch,self.root/epoch,self.output,self.lock,
                         timeout=0.1,poll_interval=0.001,missing_grace=0)

    def test_input_content_address_and_restore_after_local_loss(self):
        self.source.unlink()
        for epoch in ("first","replacement"):
            e.materialize_inputs(self.vol,e.load_request(self.vol,self.cfg,self.identifier),self.root/epoch)
            path=self.root/epoch/e.input_name(self.req["inputs"]["1.image"])
            self.assertEqual(path.read_bytes(),b"original-image")

    def test_mutation_after_intent_rejected(self):
        self.source.write_bytes(b"changed")
        identifier=e.job_id(["other"],self.intent)
        with self.assertRaisesRegex(RuntimeError,"changed after intent"):
            e.stage_request(self.vol,self.cfg,identifier,self.intent,self.graph,{"1.image":"@"+str(self.source)},
                            expected_inputs={"1.image":self.req["inputs"]["1.image"]["sha256"]})
        self.assertNotIn(e.request_key(self.cfg,identifier),self.vol.objects)

    def test_corrupt_input_rejected_before_claim(self):
        self.vol.objects[e.input_key(self.req["inputs"]["1.image"])]=b"X"*len(b"original-image")
        engine=Engine(self.output,self.vol,self.req)
        with self.assertRaisesRegex(RuntimeError,"SHA-256"):
            self.run_engine(engine)
        self.assertEqual(engine.submits,0)
        self.assertEqual(e.inspect_execution(self.vol,self.req)["status"],"prepared")

    def test_claim_exclusive_and_success_recovered_in_new_epoch(self):
        engine=Engine(self.output,self.vol,self.req)
        result=self.run_engine(engine)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(engine.submits,1)
        other=Engine(self.output,self.vol,self.req)
        self.assertEqual(self.run_engine(other,"epoch2")["receipt"],result["receipt"])
        self.assertEqual(other.submits,0)
        self.assertEqual(self.vol.commits,1)

    def test_lost_native_reply_recovers_known_uuid(self):
        engine=Engine(self.output,self.vol,self.req,crash=True)
        result=self.run_engine(engine)
        self.assertEqual(result["status"],"completed")
        self.assertEqual(engine.submits,1)

    def test_claimed_old_epoch_never_resubmits(self):
        e._write_event(self.vol,self.req,"claim",epoch="dead-epoch",owner="original",prompt_id=self.identifier)
        engine=Engine(self.output,self.vol,self.req)
        state=self.run_engine(engine,"replacement")
        self.assertEqual(state["status"],"interrupted")
        self.assertEqual(engine.submits,0)
        self.assertEqual(e.inspect_execution(self.vol,self.req)["status"],"admitted")

    def test_same_epoch_missing_queue_is_uncertain_not_replayed(self):
        e._write_event(self.vol,self.req,"claim",epoch="epoch1",owner="original",prompt_id=self.identifier)
        engine=Engine(self.output,self.vol,self.req)
        result=self.run_engine(engine)
        self.assertEqual(result["status"],"uncertain")
        self.assertEqual(engine.submits,0)

    def test_recover_produced_when_ready_missing(self):
        result=self.run_engine(Engine(self.output,self.vol,self.req))
        ready=d.receipt_key("test",1,self.identifier)
        del self.vol.objects[ready]
        produced=e.inspect_execution(self.vol,self.req)
        self.assertEqual(produced["status"],"produced")
        recovered=e.finalize_produced(self.vol,self.req,produced)
        self.assertEqual(recovered["receipt"],result["receipt"])

    def test_produced_corruption_cannot_publish_ready(self):
        result=self.run_engine(Engine(self.output,self.vol,self.req))
        ready=d.receipt_key("test",1,self.identifier)
        del self.vol.objects[ready]
        file=result["receipt"]["files"][0]
        self.vol.objects[file["key"]]=b"X"*file["bytes"]
        with self.assertRaisesRegex(RuntimeError,"SHA-256"):
            e.finalize_produced(self.vol,self.req,e.inspect_execution(self.vol,self.req))
        self.assertNotIn(ready,self.vol.objects)

    def test_commit_failure_never_marks_produced(self):
        self.vol.fail_commit=True
        engine=Engine(self.output,self.vol,self.req)
        with self.assertRaisesRegex(OSError,"commit"):
            self.run_engine(engine)
        self.assertEqual(e.inspect_execution(self.vol,self.req)["status"],"submitted")
        self.assertNotIn(d.receipt_key("test",1,self.identifier),self.vol.objects)
        self.vol.fail_commit=False
        # Same live process can reconcile history without a second POST.
        self.assertEqual(self.run_engine(engine)["status"],"completed")
        self.assertEqual(engine.submits,1)

    def test_immutable_request_conflict(self):
        self.source.write_bytes(b"other")
        with self.assertRaisesRegex(RuntimeError,"Conflicting immutable"):
            e.stage_request(self.vol,self.cfg,self.identifier,self.intent,self.graph,
                            {"1.image":"@"+str(self.source)},["2"])
        self.assertEqual(e.load_request(self.vol,self.cfg,self.identifier),self.req)

    def test_real_v2_conflict_type_is_narrowly_recognized(self):
        from modal.exception import InvalidError
        key=e.event_key(self.cfg,self.identifier,"claim")
        self.assertTrue(e.is_write_conflict(InvalidError(f'path "/{key}" already exists'),key))
        self.assertFalse(e.is_write_conflict(InvalidError('permission denied'),key))
        self.assertFalse(e.is_write_conflict(InvalidError('path "/other" already exists'),key))

    def test_claim_race_has_one_winner(self):
        barrier=threading.Barrier(2)
        results=[]
        key=e.event_key(self.cfg,self.identifier,"claim")
        def claim(n):
            try:
                with self.vol.batch_upload(force=False) as batch:
                    batch.put_file(io.BytesIO(e.canonical({"owner":n})),key)
                    barrier.wait(timeout=2)
                results.append("won")
            except FileExistsError:
                results.append("lost")
        ts=[threading.Thread(target=claim,args=(n,)) for n in range(2)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertCountEqual(results,["won","lost"])

    def test_native_rejection_is_terminal_without_replay(self):
        engine=Engine(self.output,self.vol,self.req)
        engine.submit=Mock(side_effect=e.RejectedPrompt("bad graph"))
        result=self.run_engine(engine)
        self.assertEqual(result["status"],"failed")
        second=Engine(self.output,self.vol,self.req)
        self.assertEqual(self.run_engine(second,"replacement")["status"],"failed")
        self.assertEqual(second.submits,0)

    def test_sdk_builtin_timeout_is_pending(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.modal=Mock()
        client.modal.FunctionCall.from_id.return_value.get.side_effect=TimeoutError()
        client.status=Mock(side_effect=AssertionError("pending must not read ledger"))
        self.assertIsNone(client.poll(self.identifier,"fc-pending"))
        client.status.assert_not_called()

    def test_runner_reattaches_saved_call_and_downloads(self):
        result=self.run_engine(Engine(self.output,self.vol,self.req))
        root=self.root/"pipeline"
        root.mkdir()
        graph=root/"graph.json";graph.write_text(json.dumps(self.graph))
        endpoints=root/"endpoints.json";endpoints.write_text(json.dumps({"x":"https://example.invalid"}))
        task=root/"task.json"
        steps=[{"id":"s","endpoint":"x","workflow":"graph.json","delivery":self.cfg,
                "execution":"durable","set":{"1.image":"@"+str(self.source)}}]
        task.write_text(json.dumps({"name":"resume","steps":steps}))
        out=root/"out"
        jobs,_=runner._build_jobs(steps,{"x":"https://example.invalid"},root,out,root,run_name="resume")
        job=jobs["s","main"]
        identifier=job["durable_job_id"]
        runner._save_json(job["status_file"],{"status":"submitted","endpoint":"x",
            "intent_hash":job["intent_hash"],"durable_job_id":identifier,"durable_call_id":"fc-existing"})
        durable=Mock()
        durable.poll.return_value={**result,"job_id":identifier}
        durable.status.return_value=result
        delivery=Mock()
        delivery.download.side_effect=lambda receipt,item,target,deadline: d.verified_copy(
            [b"result"],target,receipt["files"][0],deadline)
        with patch("storage.execution_client.DurableClient",return_value=durable), \
             patch("storage.delivery_client.DeliveryClient",return_value=delivery), \
             patch.object(runner,"request",side_effect=AssertionError("native request")):
            runner.run_task(task,endpoints,output=out,timeout=10)
        durable.poll.assert_called_once_with(identifier,"fc-existing")
        durable.start.assert_not_called()
        durable.stage.assert_not_called()
        delivery.receipt.assert_not_called()
        self.assertEqual((out/"s/main/result.png").read_bytes(),b"result")
        self.assertEqual(json.loads(job["status_file"].read_text())["status"],"completed")

    def test_explicit_retry_uses_cloud_manifest_and_acknowledgement(self):
        from storage.execution_client import DurableClient
        self.source.unlink()
        e._write_event(self.vol,self.req,"claim",epoch="dead",owner="original",prompt_id=self.identifier)
        client=object.__new__(DurableClient)
        client.volume=self.vol
        client.config=self.cfg
        with self.assertRaisesRegex(RuntimeError,"acknowledgement"):
            client.prepare_retry(self.identifier,"retry1")
        new=client.prepare_retry(self.identifier,"retry1",allow_unknown=True)
        self.assertEqual(new,client.prepare_retry(self.identifier,"retry1",allow_unknown=True))
        self.assertNotEqual(new["job_id"],self.identifier)
        req=e.load_request(self.vol,self.cfg,new["job_id"])
        self.assertEqual(req["inputs"],self.req["inputs"])
        self.assertEqual(req["graph"],self.req["graph"])
        self.assertEqual(e.inspect_execution(self.vol,req)["status"],"prepared")
        self.assertEqual(e.inspect_execution(self.vol,self.req)["status"],"admitted")

    def test_runner_durable_never_calls_native_upload_or_prompt(self):
        folder=self.root/"job"
        job={"base":"https://example.invalid","endpoint":"test","folder":folder,"status_file":folder/"status.json",
             "entry":{},"settings":{"1.image":"@"+str(self.source)},"graph":self.graph,
             "execution":"durable","delivery":self.cfg,"intent_hash":self.intent,
             "durable_job_id":self.identifier,"output_nodes":["2"]}
        client=Mock()
        client.stage.return_value=self.req
        client.start.return_value={"status":"dispatched","call_id":"fc-test","job_id":self.identifier}
        with patch("storage.execution_client.DurableClient",return_value=client), \
             patch.object(runner,"request",side_effect=AssertionError("native POST")), \
             patch.object(runner,"upload",side_effect=AssertionError("native upload")):
            runner._submit_job(("s","j"),job,{},self.root/"task.json",self.root,{})
        state=json.loads(job["status_file"].read_text())
        self.assertEqual(state["durable_job_id"],self.identifier)
        self.assertEqual(state["durable_call_id"],"fc-test")
        self.assertEqual(job["state"],"submitted")


if __name__ == "__main__":
    unittest.main()
