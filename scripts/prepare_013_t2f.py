import csv,json,os
from pathlib import Path
root=Path(__file__).resolve().parents[1]
rows=list(csv.DictReader(open(Path(os.environ["TEMP"])/"t2f_bench_original.csv",encoding="utf-8-sig")))
script=rows[1]["ai_film_script"]
assert "INT. HOTEL SUITE" in script and "MICHAEL" in script and "KAY" in script
shots=[
("S01","Wide establishing shot of a dim hotel suite at night. A man and a woman sit across a small dinner table. Melancholic, intimate mood; subtle jazz ambience. Slow cinematic dolly inward."),
("S02","Medium close shot of the man at the dinner table. He silently finishes his meal, wipes his mouth with a napkin, then looks toward the woman with a guarded expression. Natural restrained acting."),
("S03","Over-the-shoulder shot from behind the man toward the seated woman. She looks up, startled and worried as he abruptly rises from the dinner table. Subtle natural facial reactions."),
("S04","Side medium shot. The man crosses to a chair and picks up his heavy wool coat, slipping his arms into it while avoiding eye contact with the woman seated nearby."),
("S05","Close two-shot in the dim hotel suite. The woman searches the man's face for reassurance while he slowly buttons his wool coat. Emotional tension and restrained expressions."),
("S06","Intimate close shot. The man hesitates, then leans down and briefly kisses the top of the seated woman's head. She stays still, looking deeply troubled. No exaggerated gestures."),
("S07","Static medium-wide shot. The man turns and walks out through the hotel suite doorway. The woman remains alone beside the dinner table and a half-empty glass, staring at the empty doorway. Lingering silence."),
]
jobs=[{"name":n,"set":{"140:131.prompt":p,"140:133.value":5,"92.filename_prefix":"video/013_t2f_002_"+n}} for n,p in shots]
task={"name":"013_t2f_002_h3_5s","steps":[{"id":"video","workflow":"workflows/010-minimax-h3-bf16-b200plus-suite/001-minimax-h3-fl2va-bf16/pika_h3_flex_t2v_api.json","endpoint":"pika","foreach":jobs}]}
(root/"tasks"/"013_t2f_002_h3_5s.json").write_text(json.dumps(task,ensure_ascii=False,indent=2),encoding="utf-8")
print("Prepared",len(jobs),"shots")
