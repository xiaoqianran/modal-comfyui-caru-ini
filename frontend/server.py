"""Local-only workflow control and private asset browser. No cloud management port."""
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from storage.s3 import download_via_r2, get_asset, list_assets

HOST = "127.0.0.1"
PORT = int(os.getenv("COMFY_CONSOLE_PORT", "8768"))
TASKS = {}
LOCK = threading.Lock()
RUNNING = set()
LINKS = {}

def workflows():
    items = []
    for folder in sorted((ROOT / "workflows").iterdir()):
        if not folder.is_dir() or not (folder / "models.json").is_file() or not (folder / "plugins.json").is_file():
            continue
        names = [x for x in folder.glob("*.json") if x.name.lower() not in {"models.json","plugins.json","model.json"}]
        if not names:
            continue
        config = json.loads((folder / "models.json").read_text(encoding="utf-8"))
        items.append({"id":folder.name,"gpu":config.get("gpu","L40S"),"app":"modal-"+folder.name})
    return items

def selected(name):
    item = next((x for x in workflows() if x["id"] == name), None)
    if item is None:
        raise ValueError("Unknown workflow")
    return item

def command(args, env=None, timeout=30):
    return subprocess.run(["uv","run","modal",*args], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)

def statuses():
    p = command(["app","list","--json"],timeout=30)
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-600:])
    rows = json.loads(p.stdout)
    if isinstance(rows,dict):
        rows = rows.get("apps", [])
    data = {}
    for item in rows:
        data.setdefault(item.get("description", item.get("name")), item)
    return data

def operate(taskid, workflow, action):
    task = TASKS[taskid]
    app = "modal-" + workflow
    try:
        if action == "deploy":
            env = os.environ.copy()
            env["WORKFLOW"] = workflow
            args = ["deploy","comfyui.py"]
            timeout = 7200
        elif action == "stop":
            env = None
            args = ["app","stop",app,"--yes"]
            timeout = 120
        else:
            raise ValueError("Unsupported action")
        result = subprocess.run(["uv","run","modal",*args], cwd=ROOT, env=env,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        task["log"] = (result.stdout + "\n" + result.stderr)[-100000:]
        if action == "deploy" and result.returncode == 0:
            urls = re.findall(r"https://[^\s]+\.modal\.run[^\s]*", task["log"])
            if urls:
                parsed = urlsplit(urls[-1].rstrip("),"))
                LINKS[workflow] = f"https://{parsed.netloc}/"
        if action == "stop" and result.returncode == 0:
            LINKS.pop(workflow, None)
        task["state"] = "success" if result.returncode == 0 else "failed"
    except Exception as exc:
        task["state"] = "failed"
        task["log"] += "\n" + str(exc)
    finally:
        with LOCK:
            RUNNING.discard(workflow)

class Handler(BaseHTTPRequestHandler):
    def allowed_host(self):
        return self.headers.get("Host", "") in (f"{HOST}:{PORT}", f"localhost:{PORT}")
    def send_data(self, data, status=200, mime="application/json; charset=utf-8"):
        payload = data if isinstance(data,bytes) else (data if isinstance(data,str) else json.dumps(data,ensure_ascii=False)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(payload)))
        self.send_header("X-Content-Type-Options","nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        try:
            if not self.allowed_host():
                return self.send_data({"error":"Invalid host"},403)
            path = urlsplit(self.path)
            query = parse_qs(path.query)
            if path.path == "/":
                return self.send_data((Path(__file__).parent / "index.html").read_bytes(),mime="text/html; charset=utf-8")
            # Serve only explicitly allowed frontend modules, never project files.
            if re.fullmatch(r"/(?:css|js)/[a-zA-Z0-9_-]+\.(?:css|js)", path.path):
                static_file = Path(__file__).parent / path.path.lstrip("/")
                if static_file.is_file():
                    mime = "text/css; charset=utf-8" if static_file.suffix == ".css" else "text/javascript; charset=utf-8"
                    return self.send_data(static_file.read_bytes(), mime=mime)
                return self.send_error(404)
            if path.path == "/api/workflows":
                return self.send_data(workflows())
            if path.path == "/api/status":
                return self.send_data({"apps":statuses(),"links":LINKS})
            if path.path.startswith("/api/task/"):
                return self.send_data(TASKS.get(path.path.rsplit("/",1)[-1],{"state":"not_found"}))
            if path.path == "/api/logs":
                wf = selected(query.get("workflow",[""])[0])
                output = command(["app","logs",wf["app"],"--tail","100"],timeout=30)
                log = output.stdout or output.stderr
                if output.returncode != 0 and "No App with name" in log:
                    log = "该工作流尚未部署到 Modal，请先点击「部署」。"
                return self.send_data({"log":log,"ok":output.returncode==0})
            if path.path == "/api/assets":
                source = query.get("source",["r2"])[0]
                workflow = query.get("workflow",[""])[0]
                if workflow:
                    selected(workflow)
                return self.send_data(list_assets(source,workflow or None))
            if path.path.startswith("/asset/"):
                bits = path.path.split("/",3)
                if len(bits) < 4:
                    raise ValueError("Missing asset path")
                source,key = bits[2],unquote(bits[3])
                item = get_asset(source,key)
                mime = {".glb":"model/gltf-binary",".gltf":"model/gltf+json"}.get(Path(key).suffix.lower(),"application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type",mime)
                self.send_header("Content-Length",str(item["ContentLength"]))
                self.send_header("X-Content-Type-Options","nosniff")
                if query.get("download") == ["1"]:
                    self.send_header("Content-Disposition","attachment; filename*=UTF-8''"+quote(Path(key).name))
                self.end_headers()
                for chunk in item["Body"].iter_chunks(chunk_size=1024*1024):
                    if chunk:
                        self.wfile.write(chunk)
                return
            self.send_error(404)
        except (BrokenPipeError,ConnectionResetError):
            pass
        except ValueError as exc:
            self.send_data({"error":str(exc)},400)
        except Exception as exc:
            self.send_data({"error":type(exc).__name__+": "+str(exc)[:300]},502)

    def do_POST(self):
        try:
            if not self.allowed_host():
                return self.send_data({"error":"Invalid host"},403)
            if self.headers.get("Origin") not in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
                return self.send_data({"error":"Invalid origin"},403)
            if self.path not in ("/api/operate", "/api/transfer"):
                return self.send_error(404)
            size = int(self.headers.get("Content-Length","0"))
            if size < 1 or size > 4096:
                return self.send_data({"error":"Invalid body size"},400)
            data = json.loads(self.rfile.read(size))
            if self.path == "/api/transfer":
                return self.send_data(download_via_r2(data.get("key", "")))
            wf = selected(data.get("workflow",""))
            action = data.get("action")
            if action not in ("deploy","stop"):
                raise ValueError("Invalid action")
            with LOCK:
                if wf["id"] in RUNNING:
                    return self.send_data({"error":"Workflow is busy"},409)
                RUNNING.add(wf["id"])
                taskid = uuid.uuid4().hex
                TASKS[taskid] = {"state":"running","log":"","workflow":wf["id"],"action":action}
            threading.Thread(target=operate,args=(taskid,wf["id"],action),daemon=True).start()
            return self.send_data({"id":taskid},202)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as exc:
            self.send_data({"error":str(exc)},400)
        except Exception as exc:
            self.send_data({"error":type(exc).__name__+": "+str(exc)[:300]},502)

class ExclusiveServer(ThreadingHTTPServer):
    # Windows may allow multiple Python servers to listen on the same port.
    # Prevent random requests from hitting an outdated process.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


if __name__ == "__main__":
    print(f"ComfyUI Console: http://{HOST}:{PORT}", flush=True)
    try:
        ExclusiveServer((HOST, PORT), Handler).serve_forever()
    except OSError as exc:
        raise SystemExit(f"Port {PORT} is already in use; close the previous frontend first: {exc}")
