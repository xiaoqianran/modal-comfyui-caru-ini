import csv,json,os,re
from pathlib import Path
from collections import Counter
root=Path(__file__).resolve().parents[1]
csvfile=Path(os.environ["TEMP"])/"MovieGenVideoBenchWithTag.csv"
rows=list(csv.DictReader(csvfile.open(encoding="utf-8-sig",newline="")))
assert len(rows)==1003
groups=[("emotion",lambda x:"human - emotion" in x["concept"]),("mouth",lambda x:"human - mouth" in x["concept"]),("camera",lambda x:"camera motion" in x["concept"]),("physics",lambda x:"physics" in x["concept"]),("high_motion",lambda x:x["motion_level"]=="high")]
used=set();selected=[]
for group,predicate in groups:
 candidates=[(i,x) for i,x in enumerate(rows) if predicate(x) and i not in used]
 # prefer all motion levels where possible, deterministic
 candidates.sort(key=lambda v:({"low":0,"medium":1,"high":2}.get(v[1]["motion_level"],3),v[0]))
 chosen=[]
 for m in ("low","medium","high"):
  for i,x in candidates:
   if x["motion_level"]==m and i not in used:
    chosen.append((i,x));used.add(i);break
 for i,x in candidates:
  if len(chosen)>=4:break
  if i not in used:chosen.append((i,x));used.add(i)
 assert len(chosen)==4,group
 selected.extend((group,i,x) for i,x in chosen)
template=json.loads((root/"tasks"/"017_animeshooter_seed_h3_5s.json").read_text(encoding="utf-8"))
wf=template["steps"][0]["workflow"];endpoint=template["steps"][0]["endpoint"]
def make_task(variant):
 jobs=[]
 for n,(group,i,x) in enumerate(selected,1):
  original=x["prompt"].strip()
  if variant=="raw":
   prompt=original
  else:
   prompt=("integrated_multimodal_description: [Shot 1] "+original+
   " Preserve the exact subjects, setting, actions, and visual style in the source description. "
   "Single continuous five-second shot with clear spatial continuity, anatomically consistent subjects and coherent motion. "
   "Stage the principal action with a readable beginning, middle and end; natural physical timing and restrained, purposeful camera movement. "
   "Avoid additional characters, cuts, overlays or invented story events.\n\noverall_soundscape: "
   "Subtle realistic ambience and synchronized diegetic sounds consistent with the described scene, without intelligible dialogue.\n\nnon_diegetic_music: None.")
  jobs.append({"name":f"S{n:02d}","set":{"140:131.prompt":prompt,"140:133.value":5,"92.filename_prefix":f"video/{'019_moviegenbench_raw' if variant=='raw' else '020_moviegenbench_h3skill'}_S{n:02d}"}})
 name="019_moviegenbench_raw_20" if variant=="raw" else "020_moviegenbench_h3skill_20"
 task={"name":name,"steps":[{"id":"video","workflow":wf,"endpoint":endpoint,"foreach":jobs}]}
 out=root/"tasks"/(name+".json")
 assert not out.exists(),out
 out.write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding="utf-8")
 return name
names=[make_task(v) for v in ("raw","skill")]
manifest=[{"shot":f"S{n:02d}","source_row_1based":i+2,"concept":x["concept"],"motion_level":x["motion_level"],"group":group,"original_prompt":x["prompt"]} for n,(group,i,x) in enumerate(selected,1)]
(root/"tasks"/"019_020_moviegenbench_20_manifest.json").write_text(json.dumps({"source":"facebookresearch/MovieGenBench benchmark/MovieGenVideoBenchWithTag.csv","total_source_rows":len(rows),"selection":manifest,"tasks":names,"notes":"Original vs H3 adapted prompts, same model, duration and endpoint. H3 adds soundscape/music none; comparison is not strictly text-only due to added audio instructions."},ensure_ascii=False,indent=2),encoding="utf-8")
print("CREATED",names,"SAMPLES",len(manifest),"MOTION",dict(Counter(x["motion_level"] for _,_,x in selected)))
