import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = ROOT / "tasks" / "024_midnight_unread_h3skill_12.json"
task = json.loads(src.read_text(encoding="utf-8"))
manifest = json.loads((ROOT / "tasks" / "023_midnight_unread_manifest.json").read_text(encoding="utf-8"))
assert len(task["steps"][0]["foreach"]) == len(manifest["shots"]) == 12
assert task["steps"][0]["endpoint"] == "pika"

base = (
    "Modern minimalist Chinese slice-of-life webtoon, flat 2D manhua illustration, "
    "very thin uniform dark-brown outlines, limited two-tone cel shading, airy negative space, "
    "soft low-saturation slate blue, warm cream, dusty mauve and muted charcoal palette; "
    "matte fills, minimal gradients and natural adult face proportions. "
    "Small subtle eye expressions, restrained gestures and clear silhouette staging. "
    "Simple but accurate modern office furniture, glass doors and elevator geometry; "
    "background architecture uses sparse clean lines and simplified reflections. "
    "A cohesive hand-drawn vertical-scroll manhua aesthetic framed as a 16:9 horizontal animation. "
    "No 3D, no photorealistic skin, no painterly rendering, no glossy highlights, "
    "no hyper-detailed textures, no chibi, no watermarks, no text overlays. "
)
present = (
    "Character design lock: LIN WAN, Chinese woman age 26 with straight shoulder-length black hair "
    "parted to one side, slender build, dark almond eyes, beige blouse beneath a neat navy blazer, "
    "charcoal pleated skirt; GU YAN, Chinese man age 28 with neatly parted short black hair, "
    "tall lean build, dark narrow eyes, charcoal long trench coat over a black turtleneck. "
    "The present-day office corridor uses a cool desaturated blue-gray nighttime palette. "
)
flashback = (
    "The same facial identities as the present-day scene, but LIN WAN wears a simple cream cardigan "
    "and GU YAN wears a white collared shirt. The subway platform has a warm cream and peach palette. "
)
items=[]
for index, job in enumerate(task["steps"][0]["foreach"], 1):
    name = f"S{index:02d}"
    assert job["name"] == name
    old = job["set"]["140:131.prompt"]
    marker = "At 0.00-1.00 seconds:"
    assert marker in old
    tail = old[old.index(marker):]
    new = ("integrated_multimodal_description: [Shot 1] " + base +
           (flashback if index == 8 else present) +
           "Keep every person's face, hairstyle, clothing, color palette and body proportions "
           "stable during the entire shot. One continuous shot without cuts. " + tail)
    assert new.count("integrated_multimodal_description:")==1
    assert new.count("overall_soundscape:")==1
    assert new.count("non_diegetic_music:")==1
    assert new.index("integrated_multimodal_description:") < new.index("overall_soundscape:") < new.index("non_diegetic_music:")
    items.append({"name":name,"set":{"140:131.prompt":new,"140:133.value":5,
      "92.filename_prefix":f"video/025_midnight_minimal_webtoon_h3_S{index:02d}"}})
    manifest["shots"][index-1]["h3_minimal_webtoon_prompt"]=new

newtask={"name":"025_midnight_unread_minimal_daily_manhua_h3skill_12",
    "steps":[{"id":"video","endpoint":"pika","workflow":task["steps"][0]["workflow"],"foreach":items}]}
out=ROOT/"tasks"/"025_midnight_unread_minimal_daily_manhua_h3skill_12.json"
meta=ROOT/"tasks"/"025_midnight_unread_minimal_daily_manifest.json"
assert not out.exists() and not meta.exists(), "Refusing overwrite of an existing comparison run"
out.write_text(json.dumps(newtask,ensure_ascii=False,indent=2),encoding="utf-8")
meta.write_text(json.dumps({
    "series":"零点未读",
    "variant":"极简现代日常条漫",
    "base_task":"024_midnight_unread_h3skill_12",
    "skill":"MiniMax H3 official h3-prompt-writing / T2VA",
    "characters":manifest["characters"],
    "duration_seconds":60,
    "format":"16:9",
    "shots":manifest["shots"]
},ensure_ascii=False,indent=2),encoding="utf-8")
print("READY",out.name,"SHOTS",len(items),"ENDPOINT",newtask["steps"][0]["endpoint"])
print("STYLE",base[:170])
