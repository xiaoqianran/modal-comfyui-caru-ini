import json, urllib.request, argparse, time
from pathlib import Path
URL="https://xiaoyueliangqqq--comfyui-1.us-east.modal.run"
UI=Path(__file__).resolve().parents[1]/"workflows/001-minimax-h3-fl2va-bf16/video_pika_h3_flex_t2v.json"
def get(url,path,data=None):
    payload=None if data is None else json.dumps(data).encode()
    with urllib.request.urlopen(urllib.request.Request(url+path,data=payload,headers={"Content-Type":"application/json"} if payload else {}),timeout=60) as r:return json.load(r)
def main():
    p=argparse.ArgumentParser()
    p.add_argument("--api",type=Path,help="Exported API-format JSON")
    p.add_argument("--submit",action="store_true")
    p.add_argument("--url",default=URL)
    a=p.parse_args()
    w=json.loads(UI.read_text(encoding="utf-8"))
    nodes=w["nodes"]+sum((s["nodes"] for s in w.get("definitions",{}).get("subgraphs",[])),[])
    def vals(t):return [n.get("widgets_values",[]) for n in nodes if n["type"]==t]
    sigmas=vals("ManualSigmas")[0][0].replace(" ","").split(",")
    print("workflow_id",w["id"],"steps",len(sigmas)-1,"sampler",vals("KSamplerSelect"),"LoRA",vals("LoraLoaderModelOnly"))
    q=get(a.url,"/queue"); print("remote queue counts",{k:len(v) if isinstance(v,list) else str(type(v)) for k,v in q.items()})
    if not a.submit:
        print("Inspect-only. Export API JSON from ComfyUI, then --api file.json --submit")
        return
    if not a.api:p.error("--submit requires --api")
    workflow=json.loads(a.api.read_text(encoding="utf-8"))
    if "nodes" in workflow:p.error("Expected API-format, got UI-format JSON")
    result=get(a.url,"/prompt",{"prompt":workflow})
    print("submitted",result)
    pid=result["prompt_id"]
    for _ in range(240):
        h=get(a.url,"/history/"+pid)
        if pid in h:
            print(json.dumps(h[pid],ensure_ascii=False)[:8000])
            return
        time.sleep(5)
    raise TimeoutError(pid)
if __name__=="__main__":main()


