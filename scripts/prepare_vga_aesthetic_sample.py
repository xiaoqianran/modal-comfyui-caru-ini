import csv, json, os
from pathlib import Path
root=Path(__file__).resolve().parents[1]
source=Path(os.environ["TEMP"])/"vga_aesthetic_200.csv"
rows=list(csv.DictReader(source.open(encoding="utf-8-sig",newline="")))
assert len(rows)==200
work="workflows/010-minimax-h3-bf16-b200plus-suite/001-minimax-h3-fl2va-bf16/pika_h3_flex_t2v_api.json"
selected=[rows[i] for i in range(1,200,10)]
for b in range(4):
    batch=selected[b*5:b*5+5]
    jobs=[]
    for entry in batch:
        n=int(entry["编号"])
        jobs.append({"name":f"A{n:03d}","set":{"140:131.prompt":entry["en_prompt"],"140:133.value":5,"92.filename_prefix":f"video/012_vga_aesthetic_A{n:03d}"}})
    obj={"name":f"012_vga_aesthetic_h3_5s_batch_{b+1:02d}","steps":[{"id":"video","workflow":work,"endpoint":"pika","foreach":jobs}]}
    p=root/"tasks"/f"012_vga_aesthetic_h3_5s_batch_{b+1:02d}.json"
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(p.name,[(x["name"],x["set"]["140:131.prompt"][:52]) for x in jobs])
manifest=root/"tasks"/"012_vga_aesthetic_h3_5s_sample.csv"
with manifest.open("w",encoding="utf-8-sig",newline="") as f:
    writer=csv.DictWriter(f,fieldnames=["id","dimension","prompt","en_prompt"])
    writer.writeheader()
    for r in selected:writer.writerow({"id":r["编号"],"dimension":r["各组合维度"],"prompt":r["Prompt"],"en_prompt":r["en_prompt"]})
print("manifest",manifest)
