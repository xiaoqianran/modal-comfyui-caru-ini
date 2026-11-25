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
        prefix = Path(graph["2"]["inputs"]["filename_prefix"])
        p = self.root / "test" / prefix.parent / (prefix.name + ".png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"result")
        self.record = {"status": {"status_str": "success", "completed": True},
                       "outputs": {"2": {"images": [{"filename": p.name, "subfolder": "" if prefix.parent == Path(".") else prefix.parent.as_posix(),
                                                    "type": "output"}]}}}
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

    def test_output_namespace_is_unique_and_preserves_original_graph(self):
        other = e.job_id(["other"], self.intent)
        graph = json.loads(json.dumps(self.graph))
        graph["2"]["inputs"]["filename_prefix"] = "video/scene/same"
        first = e.scoped_output_graph(graph, self.identifier, ["2"])
        second = e.scoped_output_graph(graph, other, ["2"])
        self.assertEqual(first["2"]["inputs"]["filename_prefix"],
                         f"video/{e.OUTPUT_NAMESPACE}/{self.identifier}/scene/same")
        self.assertEqual(second["2"]["inputs"]["filename_prefix"],
                         f"video/{e.OUTPUT_NAMESPACE}/{other}/scene/same")
        self.assertEqual(graph["2"]["inputs"]["filename_prefix"], "video/scene/same")
        self.assertNotEqual(first, second)

    def test_unsafe_or_linked_output_prefix_rejected_before_admission(self):
        for prefix in (["3", 0], "../shared", "/absolute", "_durable_outputs/other/foo"):
            graph = json.loads(json.dumps(self.graph))
            graph["2"]["inputs"]["filename_prefix"] = prefix
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                e.scoped_output_graph(graph, self.identifier, ["2"])
        graph = json.loads(json.dumps(self.graph))
        del graph["2"]["inputs"]["filename_prefix"]
        with self.assertRaisesRegex(ValueError, "literal filename_prefix"):
            e.scoped_output_graph(graph, self.identifier, ["2"])

    def test_native_output_escape_cannot_publish_success(self):
        engine = Engine(self.output, self.vol, self.req)
        raw_submit = engine.submit
        def bypass(graph, identifier):
            original = json.loads(json.dumps(graph))
            original["2"]["inputs"]["filename_prefix"] = "unscoped"
            return raw_submit(original, identifier)
        engine.submit = bypass
        with self.assertRaisesRegex(RuntimeError, "escaped durable job namespace"):
            self.run_engine(engine)
        self.assertEqual(e.inspect_execution(self.vol, self.req)["status"], "submitted")
        self.assertNotIn(d.receipt_key("test", 1, self.identifier), self.vol.objects)

    def test_two_attempts_same_prefix_have_distinct_receipt_keys(self):
        other = e.job_id(["other"], self.intent)
        request = e.stage_request(self.vol, self.cfg, other, self.intent, self.graph,
                                  {"1.image": "@"+str(self.source)}, ["2"])
        first = self.run_engine(Engine(self.output, self.vol, self.req))
        second = e.execute(self.vol, request, Engine(self.output,self.vol,request),
                           "epoch2", self.root/"epoch2", self.output, self.lock,
                           timeout=0.1, poll_interval=0.001, missing_grace=0)
        a = first["receipt"]["files"][0]["key"]
        b = second["receipt"]["files"][0]["key"]
        self.assertNotEqual(a, b)
        self.assertIn(self.identifier, a)
        self.assertIn(other, b)
        self.assertEqual(self.vol.objects[a], b"result")
        self.assertEqual(self.vol.objects[b], b"result")

    def test_video_audio_inputs_restore_exact_bytes_and_deduplicate(self):
        video=self.root/"clip.MP4"; video.write_bytes(b"video-bytes")
        audio=self.root/"voice.WAV"; audio.write_bytes(b"audio-bytes")
        graph={
            "1":{"class_type":"LoadVideo","inputs":{"file":"placeholder.mp4"}},
            "2":{"class_type":"LoadAudio","inputs":{"audio":"placeholder.wav"}},
            "3":{"class_type":"LoadAudio","inputs":{"audio":"placeholder.mp4"}},
            "4":{"class_type":"SaveVideo","inputs":{"video":["1",0],"filename_prefix":"video/test"}}
        }
        ident=e.job_id(["media"],self.intent)
        req=e.stage_request(self.vol,self.cfg,ident,self.intent,graph,
                            {"1.file":"@"+str(video),"2.audio":"@"+str(audio),
                             "3.audio":"@"+str(video)},["4"])
        self.assertEqual(req["inputs"]["1.file"],req["inputs"]["3.audio"])
        self.assertEqual(req["inputs"]["1.file"]["extension"],".mp4")
        self.assertEqual(req["inputs"]["2.audio"]["extension"],".wav")
        self.assertEqual(req["graph"]["1"]["inputs"]["file"],e.input_name(req["inputs"]["1.file"]))
        self.assertEqual(len({e.input_key(v) for v in req["inputs"].values()}),2)
        video.unlink();audio.unlink()
        for epoch in ("media-a","media-b"):
            root=self.root/epoch
            e.materialize_inputs(self.vol,e.load_request(self.vol,self.cfg,ident),root)
            for field,expected in (("1.file",b"video-bytes"),("2.audio",b"audio-bytes"),
                                   ("3.audio",b"video-bytes")):
                self.assertEqual((root/e.input_name(req["inputs"][field])).read_bytes(),expected)

    def test_media_format_mismatch_and_unverified_loader_are_rejected(self):
        for cls,field,ext in (("LoadVideo","file",".wav"),("LoadAudio","audio",".png"),
                              ("LoadImage","image",".mp4"),("CustomLoader","path",".mp4"),
                              ("LoadVideo","file",".exe")):
            source=self.root/("fixture"+ext);source.write_bytes(b"test")
            graph={"1":{"class_type":cls,"inputs":{field:"placeholder"}},
                   "2":{"class_type":"SaveImage","inputs":{"images":["1",0],"filename_prefix":"test"}}}
            ident=e.job_id([cls,ext],self.intent)
            before=dict(self.vol.objects)
            with self.subTest(cls=cls,ext=ext), self.assertRaisesRegex(ValueError,"Unsupported durable input"):
                e.stage_request(self.vol,self.cfg,ident,self.intent,graph,
                                {"1."+field:"@"+str(source)},["2"])
            self.assertEqual(self.vol.objects,before)

    def test_empty_media_and_changed_hash_publish_nothing(self):
        source=self.root/"audio.wav"
        for raw,expected,error in ((b"",None,"Invalid persisted input evidence"),
                                   (b"changed","a"*64,"Input changed")):
            source.write_bytes(raw)
            before=dict(self.vol.objects)
            with self.subTest(raw=raw), self.assertRaisesRegex((ValueError,RuntimeError),error):
                e.stage_input(self.vol,source,expected_sha256=expected)
            self.assertEqual(self.vol.objects,before)

    def test_tampered_media_reference_is_rejected_on_manifest_load(self):
        graph={"1":{"class_type":"LoadAudio","inputs":{"audio":"test.wav"}},
               "2":{"class_type":"SaveAudio","inputs":{"audio":["1",0],"filename_prefix":"audio/test"}}}
        audio=self.root/"test.wav";audio.write_bytes(b"wave")
        ident=e.job_id(["audio"],self.intent)
        req=e.stage_request(self.vol,self.cfg,ident,self.intent,graph,
                            {"1.audio":"@"+str(audio)},["2"])
        req["graph"]["1"]["class_type"]="LoadVideo"
        with self.assertRaisesRegex(ValueError,"Unsupported durable input"):
            e.validate_request(req,self.cfg,ident)

    def test_corrupt_media_does_not_reach_native_submission(self):
        audio=self.root/"sound.flac";audio.write_bytes(b"wave-data")
        graph={"1":{"class_type":"LoadAudio","inputs":{"audio":"placeholder.flac"}},
               "2":{"class_type":"SaveAudio","inputs":{"audio":["1",0],"filename_prefix":"audio/test"}}}
        ident=e.job_id(["corrupt-audio"],self.intent)
        req=e.stage_request(self.vol,self.cfg,ident,self.intent,graph,
                            {"1.audio":"@"+str(audio)},["2"])
        self.vol.objects[e.input_key(req["inputs"]["1.audio"])]=b"X"*9
        engine=Mock()
        with self.assertRaisesRegex(RuntimeError,"SHA-256"):
            e.execute(self.vol,req,engine,"epoch",self.root/"epoch",self.output,self.lock,
                      timeout=0.1,poll_interval=0.001,missing_grace=0)
        engine.submit.assert_not_called()
        self.assertEqual(e.inspect_execution(self.vol,req)["status"],"prepared")

    def test_media_requires_new_cpu_probe_before_upload(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient);client.volume=self.vol;client.config=self.cfg
        client.preflight=Mock(return_value={"execution_schema":1})
        audio=self.root/"sound.wav";audio.write_bytes(b"sound")
        graph={"1":{"class_type":"LoadAudio","inputs":{"audio":"placeholder"}},
               "2":{"class_type":"SaveAudio","inputs":{"audio":["1",0],"filename_prefix":"test"}}}
        ident=e.job_id(["probe"],self.intent)
        before=dict(self.vol.objects)
        with self.assertRaisesRegex(RuntimeError,"redeploy first"):
            client.stage(ident,self.intent,graph,{"1.audio":"@"+str(audio)},["2"])
        self.assertEqual(self.vol.objects,before)
        client.preflight.return_value={"execution_schema":1,"execution_media_inputs":1}
        req=client.stage(ident,self.intent,graph,{"1.audio":"@"+str(audio)},["2"])
        self.assertEqual(req["inputs"]["1.audio"]["sha256"],hashlib.sha256(b"sound").hexdigest())

    def test_upstream_media_reference_resolves_by_type_and_rejects_ambiguity(self):
        video=self.root/"out.mp4";audio=self.root/"out.flac";other=self.root/"out.wav"
        complete={"first":{"main":[video,audio]}}
        self.assertEqual(runner._input_value("$first.video",{},complete),"@"+str(video.resolve()))
        self.assertEqual(runner._input_value("$first.main.audio",{},complete),"@"+str(audio.resolve()))
        complete["first"]["main"].append(other)
        with self.assertRaisesRegex(ValueError,"expected one audio"):
            runner._input_value("$first.audio",{},complete)

    def test_upstream_media_hash_is_checked_before_staging(self):
        from storage.execution_client import DurableClient
        audio=self.root/"collected.flac";audio.write_bytes(b"original")
        evidence=runner._file_evidence(audio)
        (self.root/"status.json").write_text(json.dumps({"status":"completed","files":[str(audio.resolve())],
            "artifacts":{audio.name:evidence}}),encoding="utf-8")
        audio.write_bytes(b"tampered")
        graph={"1":{"class_type":"LoadAudio","inputs":{"audio":"placeholder"}},
               "2":{"class_type":"SaveAudio","inputs":{"audio":["1",0],"filename_prefix":"test"}}}
        ident=e.job_id(["upstream"],self.intent)
        job={"base":"https://example.invalid","endpoint":"test","folder":self.root/"downstream",
             "status_file":self.root/"downstream/status.json","entry":{},
             "settings":{"1.audio":"$first.audio"},"graph":graph,"execution":"durable",
             "delivery":self.cfg,"intent_hash":self.intent,"durable_job_id":ident,"output_nodes":["2"]}
        client=object.__new__(DurableClient);client.volume=self.vol;client.config=self.cfg
        client.preflight=Mock(return_value={"execution_schema":1,"execution_media_inputs":1})
        client.start=Mock(side_effect=AssertionError("unexpected dispatch"))
        before=dict(self.vol.objects)
        with patch("storage.execution_client.DurableClient",return_value=client):
            with self.assertRaisesRegex(RuntimeError,"upstream delivery"):
                runner._submit_job(("s","j"),job,{"first":{"main":[audio]}},self.root/"task.json",self.root,{})
        self.assertEqual(self.vol.objects,before)
        client.start.assert_not_called()
        (self.root/"status.json").unlink()
        with self.assertRaisesRegex(RuntimeError,"Missing upstream artifact evidence"):
            runner._upstream_input_hash(audio)

    def test_native_http_still_rejects_video_and_audio_upload(self):
        for ext in (".mp4",".wav"):
            path=self.root/("file"+ext);path.write_bytes(b"data")
            with self.subTest(ext=ext), patch.object(runner,"request",side_effect=AssertionError("HTTP")):
                with self.assertRaisesRegex(ValueError,"Non-image inputs"):
                    runner.upload("https://example.invalid",path)

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

    def test_start_existing_claim_does_not_wake_gpu(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.config=self.cfg
        client.status=Mock(return_value={"status":"submitted","job_id":self.identifier})
        client.preflight=Mock(side_effect=AssertionError("GPU preflight must not run"))
        client._model=Mock(side_effect=AssertionError("GPU must not spawn"))
        state=client.start(self.identifier)
        self.assertEqual(state["status"],"observing")
        self.assertEqual(state["ledger_status"],"submitted")
        client.preflight.assert_not_called()
        client._model.assert_not_called()

    def test_explicit_reconcile_dispatches_same_identity(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.config=self.cfg
        client.status=Mock(return_value={"status":"submitted","job_id":self.identifier})
        client.preflight=Mock()
        client._model=Mock()
        spawn=client._model.return_value.execute_durable.spawn
        spawn.return_value.object_id="fc-reconcile"
        state=client.start(self.identifier,reconcile=True)
        self.assertEqual(state["status"],"dispatched")
        spawn.assert_called_once_with(self.identifier,1,self.cfg["version"])

    def test_cli_claimed_resume_observes_even_with_endpoint(self):
        import recover_job
        cfg_path=self.root/"config.json"
        cfg_path.write_text(json.dumps(self.cfg),encoding="utf-8")
        client=Mock()
        client.status.return_value={"status":"submitted","job_id":self.identifier}
        client.start.return_value={"status":"observing","job_id":self.identifier}
        client.poll.return_value={"status":"completed","job_id":self.identifier}
        args=["recover_job.py","resume","--config",str(cfg_path),"--job",self.identifier,
              "--endpoint","https://example.invalid","--timeout","5"]
        with patch.object(recover_job,"DurableClient",return_value=client), patch("sys.argv",args):
            recover_job.main()
        client.start.assert_called_once_with(self.identifier,reconcile=False)
        client.poll.assert_called_once_with(self.identifier,None)

    def test_dispatch_existing_claim_persists_observation_without_call_id(self):
        client=Mock()
        client.start.return_value={"status":"observing","job_id":self.identifier,
                                   "ledger_status":"admitted","previous_call_id":None,
                                   "observer_call_status":"not_attached"}
        job={"endpoint":"test","base":"https://example.invalid","delivery":self.cfg,
             "intent_hash":self.intent,"durable_job_id":self.identifier,
             "status_file":self.root/"status.json","state":"pending"}
        runner._dispatch_durable(("s","j"),job,client)
        self.assertTrue(job["durable_observe_only"])
        state=json.loads(job["status_file"].read_text())
        self.assertEqual(state["status"],"observing")
        self.assertNotIn("durable_call_id",state)
        client.start.assert_called_once_with(self.identifier)

    def test_authoritative_terminal_evidence_beats_observer_interruption(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.modal=Mock()
        client.modal.FunctionCall.from_id.return_value.get.return_value={
            "job_id":self.identifier,"status":"interrupted"}
        for state in ("completed","failed"):
            record={"status":state,"job_id":self.identifier}
            client.status=Mock(return_value=record)
            self.assertEqual(client.poll(self.identifier,"fc-observer"),record)

    def test_cli_switches_from_finished_call_to_ledger(self):
        import recover_job
        cfg_path=self.root/"config.json"
        cfg_path.write_text(json.dumps(self.cfg),encoding="utf-8")
        client=Mock()
        client.status.return_value={"status":"submitted","job_id":self.identifier}
        client.poll.side_effect=[{"status":"observing","job_id":self.identifier},
                                 {"status":"completed","job_id":self.identifier}]
        args=["recover_job.py","resume","--config",str(cfg_path),"--job",self.identifier,
              "--call-id","fc-observer","--timeout","5"]
        with patch.object(recover_job,"DurableClient",return_value=client), \
             patch("sys.argv",args), patch.object(recover_job.time,"sleep"):
            recover_job.main()
        self.assertEqual([c.args for c in client.poll.call_args_list],
                         [(self.identifier,"fc-observer"),(self.identifier,None)])
        client.start.assert_not_called()

    def test_duplicate_observer_follows_ledger_to_completed(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.modal=Mock()
        call=client.modal.FunctionCall.from_id.return_value
        call.get.return_value={"job_id":self.identifier,"status":"interrupted","error":"other epoch"}
        active={"status":"submitted","job_id":self.identifier,"epoch":"owner"}
        complete={**active,"status":"completed","receipt":{}}
        client.status=Mock(side_effect=[active,active,complete])
        observed=client.poll(self.identifier,"fc-duplicate")
        self.assertEqual(observed["status"],"observing")
        self.assertEqual(observed["ledger_status"],"submitted")
        self.assertEqual(observed["observer_call_status"],"interrupted")
        self.assertIsNone(client.poll(self.identifier,None))
        self.assertEqual(client.poll(self.identifier,None),complete)
        call.get.assert_called_once_with(timeout=0)
        client.modal.FunctionCall.from_id.assert_called_once_with("fc-duplicate")

    def test_claimed_call_transport_error_is_observation_not_failure(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.modal=Mock()
        client.modal.FunctionCall.from_id.return_value.get.side_effect=OSError("connection lost")
        active={"status":"admitted","job_id":self.identifier}
        failed={**active,"status":"failed","error":"native execution failed"}
        client.status=Mock(side_effect=[active,failed])
        self.assertEqual(client.poll(self.identifier,"fc-lost")["status"],"observing")
        self.assertEqual(client.poll(self.identifier,None),failed)

    def test_unclaimed_call_error_remains_uncertain(self):
        from storage.execution_client import DurableClient
        client=object.__new__(DurableClient)
        client.modal=Mock()
        client.modal.FunctionCall.from_id.return_value.get.side_effect=OSError("connection lost")
        client.status=Mock(return_value={"status":"prepared","job_id":self.identifier})
        self.assertEqual(client.poll(self.identifier,"fc-lost")["status"],"uncertain")
        with self.assertRaisesRegex(RuntimeError,"claim is missing"):
            client.poll(self.identifier,None)

    def test_runner_observing_state_survives_restart(self):
        job={"endpoint":"test","base":"https://example.invalid","delivery":self.cfg,
             "intent_hash":self.intent,"durable_job_id":self.identifier,
             "durable_call_id":"fc-observer","status_file":self.root/"status.json"}
        result={"status":"observing","ledger_status":"submitted",
                "observer_call_status":"interrupted","previous_call_id":"fc-observer"}
        self.assertIsNone(runner._apply_durable_result(("s","j"),job,result))
        status=json.loads(job["status_file"].read_text())
        self.assertEqual(status["status"],"observing")
        self.assertEqual(status["durable_call_id"],"fc-observer")
        self.assertTrue(job["durable_observe_only"])
        client=Mock()
        client.poll.return_value=None
        job["execution"]="durable"
        with patch("storage.execution_client.DurableClient",return_value=client):
            self.assertIsNone(runner._read_outputs(("s","j"),job))
            client.poll.assert_called_once_with(self.identifier,None)
            job["durable_observe_deadline"]=0
            with self.assertRaises(TimeoutError):
                runner._read_outputs(("s","j"),job)
        self.assertEqual(json.loads(job["status_file"].read_text()),status)

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

    def test_runner_resumes_saved_observation_without_new_dispatch(self):
        result=self.run_engine(Engine(self.output,self.vol,self.req))
        root=self.root/"observe-pipeline"
        root.mkdir()
        (root/"graph.json").write_text(json.dumps(self.graph))
        endpoints=root/"endpoints.json"
        endpoints.write_text(json.dumps({"x":"https://example.invalid"}))
        task=root/"task.json"
        steps=[{"id":"s","endpoint":"x","workflow":"graph.json","delivery":self.cfg,
                "execution":"durable","set":{"1.image":"@"+str(self.source)}}]
        task.write_text(json.dumps({"name":"observe","steps":steps}))
        out=root/"out"
        jobs,_=runner._build_jobs(steps,{"x":"https://example.invalid"},root,out,root,run_name="observe")
        job=jobs["s","main"]
        identifier=job["durable_job_id"]
        runner._save_json(job["status_file"],{"status":"observing","endpoint":"x",
            "intent_hash":job["intent_hash"],"durable_job_id":identifier,"durable_call_id":"fc-observer"})
        durable=Mock()
        durable.poll.side_effect=[None,{**result,"job_id":identifier}]
        durable.status.return_value=result
        delivery=Mock()
        delivery.download.side_effect=lambda receipt,item,target,deadline: d.verified_copy(
            [b"result"],target,receipt["files"][0],deadline)
        with patch("storage.execution_client.DurableClient",return_value=durable), \
             patch("storage.delivery_client.DeliveryClient",return_value=delivery), \
             patch.object(runner,"request",side_effect=AssertionError("native request")):
            runner.run_task(task,endpoints,output=out,timeout=10)
        self.assertEqual(durable.poll.call_count,2)
        for call in durable.poll.call_args_list:
            self.assertEqual(call.args,(identifier,None))
        durable.start.assert_not_called()
        durable.stage.assert_not_called()
        self.assertEqual((out/"s/main/result.png").read_bytes(),b"result")

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
