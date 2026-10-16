"""Private R2 GLB gallery: http://127.0.0.1:8766."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

import boto3
from dotenv import load_dotenv

load_dotenv()
BUCKET = "modal-studio-assets"
PREFIX = "comfyui/"
s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com"),
    aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    region_name="auto",
)

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>ComfyUI · R2 GLB Gallery</title>
<script type="module" src="https://ajax.googleapis.com/ajax/libs/model-viewer/4.1.0/model-viewer.min.js"></script>
<style>
body{margin:0;background:#131820;color:#eee;font:14px system-ui;display:flex;height:100vh}
aside{width:320px;overflow:auto;padding:18px;border-right:1px solid #35404f;box-sizing:border-box}
button{display:block;background:#242e3b;border:0;color:white;margin:8px 0;padding:12px;width:100%;text-align:left;cursor:pointer;overflow-wrap:anywhere}
button:hover{background:#43546a}main{flex:1;display:flex;flex-direction:column;min-width:0}
header{padding:14px;border-bottom:1px solid #35404f}model-viewer{flex:1;width:100%;height:100%;background:#202734}
a{color:#a7d1ff}
</style>
<aside><h2>R2 · GLB</h2><button onclick="load()">刷新文件列表</button><div id="files">加载中…</div></aside>
<main><header><span id="name">选择一个 GLB 预览</span> · <a id="download" hidden>下载</a></header>
<model-viewer id="viewer" camera-controls auto-rotate shadow-intensity="1"></model-viewer></main>
<script>
async function load(){
 const files=document.getElementById("files");files.textContent="加载中…";
 try{
  const r=await fetch("/api/files");if(!r.ok)throw Error(await r.text());
  const items=await r.json();files.textContent="";
  if(!items.length)files.textContent="暂无 GLB";
  for(const item of items){
   const b=document.createElement("button");b.textContent=item.key+" ("+(item.size/1048576).toFixed(1)+" MiB)";
   b.onclick=()=>{
    const u="/glb/"+item.key.split("/").map(encodeURIComponent).join("/");
    document.getElementById("viewer").src=u;
    document.getElementById("name").textContent=item.key;
    const a=document.getElementById("download");a.href=u+"?download=1";a.hidden=false;a.download=item.key.split("/").pop();
   };files.append(b);
  }
 }catch(e){files.textContent="加载失败："+e.message}
}
load();
</script></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path)
        try:
            if route.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route.path == "/api/files":
                files = []
                for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
                    files.extend({"key": x["Key"], "size": x["Size"]} for x in page.get("Contents", []) if x["Key"].lower().endswith(".glb"))
                body = json.dumps(files, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route.path.startswith("/glb/"):
                key = unquote(route.path[len("/glb/"):])
                if not key.startswith(PREFIX) or not key.lower().endswith(".glb") or ".." in key.split("/"):
                    self.send_error(400)
                    return
                obj = s3.get_object(Bucket=BUCKET, Key=key)
                self.send_response(200)
                self.send_header("Content-Type", "model/gltf-binary")
                self.send_header("Content-Length", str(obj["ContentLength"]))
                if route.query == "download=1":
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(key.split("/")[-1]))
                self.end_headers()
                for chunk in obj["Body"].iter_chunks(chunk_size=1024 * 1024):
                    if chunk:
                        self.wfile.write(chunk)
            else:
                self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print("R2 error:", type(exc).__name__, str(exc)[:300])
            try:
                self.send_error(502, "Unable to read R2 object")
            except (BrokenPipeError, ConnectionResetError):
                pass


if __name__ == "__main__":
    print("GLB Gallery: http://127.0.0.1:8766")
    ThreadingHTTPServer(("127.0.0.1", 8766), Handler).serve_forever()

