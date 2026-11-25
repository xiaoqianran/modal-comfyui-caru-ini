"""Loopback native-API simulation, not a GPU performance test."""
import contextlib
import hashlib
import http.server
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from urllib.parse import urlsplit,parse_qs

ROOT = Path(__file__).resolve().parents[1]
import os
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
Path("outputs").mkdir(exist_ok=True)
import batch_run as new
old=types.ModuleType("baseline")
exec(compile(subprocess.check_output(["git","show","4c99194c117f6d66ed36f05d43b6c99120e0f5df:batch_run.py"]).decode("utf-8"),"baseline.py","exec"),old.__dict__)
PAYLOAD=bytes(range(256))*8192
EXPECTED=hashlib.sha256(PAYLOAD).hexdigest()
def one(impl, label):
    lock=threading.Lock()
    events=[]
    counters={"a":0,"b":0}
    def event(kind,endpoint,id=""):
        with lock:events.append((time.perf_counter(),kind,endpoint,id))
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self, body):
            if isinstance(body,dict):body=json.dumps(body).encode()
            self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            parts=self.path.split("/")
            e=parts[1]
            if parts[-1]!="prompt":raise AssertionError(self.path)
            json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                counters[e]+=1;pid=f"{e}-{counters[e]}"
            event("submit_start",e,pid);time.sleep(.25);self.send({"prompt_id":pid});event("submit_end",e,pid)
        def do_GET(self):
            path=urlsplit(self.path);parts=path.path.split("/");e=parts[1]
            if parts[2]=="history":
                pid=parts[3];event("poll",e,pid);time.sleep(.04)
                self.send({pid:{"status":{"completed":True,"status_str":"success"},"outputs":{"1":{"images":[{"filename":pid+".png","subfolder":"","type":"output"}]}}}})
            elif parts[2]=="view":
                pid=parse_qs(path.query)["filename"][0]
                event("download_start",e,pid);time.sleep(.65);self.send(PAYLOAD);event("download_end",e,pid)
            else:self.send_error(404)
    server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);base=f"http://127.0.0.1:{server.server_port}"
            (root/"g.json").write_text('{"1":{"inputs":{"prompt":""}}}')
            (root/"endpoints.json").write_text(json.dumps({"a":base+"/a","b":base+"/b"}))
            task={"name":"bench","steps":[{"id":e,"endpoint":e,"workflow":"g.json","foreach":[{"name":str(n)} for n in range(3)]} for e in ("a","b")]}
            (root/"task.json").write_text(json.dumps(task))
            start=time.perf_counter()
            with contextlib.redirect_stdout(io.StringIO()):
                output=impl.run_task(root/"task.json",root/"endpoints.json",timeout=30)
            elapsed=time.perf_counter()-start
            files=list(output.rglob("*.png"));assert len(files)==6
            assert all(hashlib.sha256(p.read_bytes()).hexdigest()==EXPECTED for p in files)
            assert counters=={"a":3,"b":3}
            starts=[(t,e,pid) for t,k,e,pid in events if k=="submit_start"];ends=[t for t,k,e,pid in events if k=="download_end"]
            overlap=any(a<=t<=b for a,k,e,pid in events if k=="download_start" for b,k2,e2,pid2 in events if k2=="download_end" and pid==pid2 for t,k3,e3,pid3 in events if k3=="poll" and pid3+".png"!=pid)
            return {"impl":label,"seconds":round(elapsed,4),"files":len(files),"bytes":sum(p.stat().st_size for p in files),"poll_during_other_download":overlap,"events":[[round(t-start,4),k,e,pid] for t,k,e,pid in events]}
    finally:server.shutdown();server.server_close();thread.join(3)
results=[]
for i in range(3):
    order=[(old,"before"),(new,"after")] if i%2==0 else [(new,"after"),(old,"before")]
    for impl,label in order:
        r=one(impl,label);results.append(r);print(label,r["seconds"],"overlap",r["poll_during_other_download"],flush=True)
Path("outputs/runtime-migration-benchmark.json").write_text(json.dumps(results,indent=2),encoding="utf-8")
import statistics
for label in ("before","after"):print(label,"median",statistics.median(x["seconds"] for x in results if x["impl"]==label))
