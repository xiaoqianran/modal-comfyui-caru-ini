import concurrent.futures
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://api.github.com/repos/stepfun-ai/Step-Video-TI2V/contents/benchmark/Step-Video-TI2V-Eval/"
GROUPS = [("ti2v_eval_ani", "H", 4), ("ti2v_eval_ani", "S", 4),
          ("ti2v_eval_ani", "V", 2), ("ti2v_eval_real", "H", 4),
          ("ti2v_eval_real", "S", 4), ("ti2v_eval_real", "V", 2)]
def get(url):
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"StepVideoTextBenchmark/1.0"})
            with urllib.request.urlopen(req, timeout=25) as response:
                return response.read()
        except Exception:
            if attempt == 4: raise
            time.sleep(1.5*(attempt+1))

def fetch_entry(entry):
    raw = get(entry["download_url"]).decode("utf-8-sig").strip()
    if not raw: raise RuntimeError("empty "+entry["path"])
    return raw

selected=[]
for group, category, count in GROUPS:
    entries=json.loads(get(BASE+group+"/"+category))
    txt=sorted((x for x in entries if x["name"].endswith(".txt")),key=lambda x:x["name"])
    if len(txt)<count: raise RuntimeError("insufficient "+group+"/"+category)
    for x in txt[:count]:
        selected.append({"id":group+"/"+category+"/"+x["name"][:-4],
            "style":"动漫" if group.endswith("ani") else "写实",
            "category":category, "source_url":x["html_url"],"entry":x})
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
    for item,original in zip(selected, executor.map(lambda x:fetch_entry(x["entry"]),selected)):
        item["original_prompt"]=original
        del item["entry"]

template=json.loads((ROOT/"tasks"/"020_moviegenbench_h3skill_20.json").read_text(encoding="utf-8"))
step=template["steps"][0]
jobs=[]
for n,item in enumerate(selected,1):
    shot=f"S{n:02d}"
    prompt=("integrated_multimodal_description: [Shot 1] "+item["original_prompt"]+
        "。严格保留原文所要求的主体、动作先后顺序、镜头移动和背景信息。"+
        ("采用动漫画面风格，角色形象和服装保持稳定。" if item["style"]=="动漫" else "采用写实画面风格，人物与物理运动自然。")+
        " 单一连续5秒镜头，主体动作清楚可见，不增加额外情节、人物、字幕或切镜。"+
        "\n\noverall_soundscape: 与场景相符的轻微自然环境声，不增加人物对白。"+
        "\n\nnon_diegetic_music: None.")
    item["h3_prompt"]=prompt
    item["shot"]=shot
    item["test_dimension"]="原始分类 "+item["category"]+"（官方目录标签；动作、镜头及一致性需要人工逐条核查）"
    jobs.append({"name":shot,"set":{"140:131.prompt":prompt,"140:133.value":5,
          "92.filename_prefix":f"video/022_step_ti2v_text_h3_{shot}"}})
task={"name":"022_step_ti2v_text_h3_20","steps":[{"id":"video",
     "endpoint":step["endpoint"],"workflow":step["workflow"],"foreach":jobs}]}
assert step["endpoint"]=="pika"
task_path=ROOT/"tasks"/"022_step_ti2v_text_h3_20.json"
manifest_path=ROOT/"tasks"/"022_step_ti2v_text_h3_20_manifest.json"
if task_path.exists() or manifest_path.exists(): raise RuntimeError("Already exists; avoid duplication")
task_path.write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding="utf-8")
manifest_path.write_text(json.dumps({"official_repo":"stepfun-ai/Step-Video-TI2V","mode":"TEXT_ONLY (not official image-conditioned evaluation)","samples":selected},ensure_ascii=False,indent=2),encoding="utf-8")
print("CREATED",len(selected))
print("TASK",task_path)
for item in selected:print(item["shot"],item["id"],json.dumps(item["original_prompt"],ensure_ascii=True),flush=True)
