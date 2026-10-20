from __future__ import annotations
import csv
import json
import signal
import time
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://shuhuaqaq--modal-002-minimax-h3-ref2va-bf16-comfyui-ui.us-east.modal.run/system_stats"
OUT = Path(__file__).resolve().parent / "logs" / "002-ref2va-bf16-gpu.csv"
OUT.parent.mkdir(exist_ok=True)
FIELDS = ["timestamp", "used_gib", "free_gib", "total_gib", "utilization_pct", "status"]
with OUT.open("a", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDS)
    if f.tell() == 0:
        writer.writeheader()
    while True:
        row = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), "status": "ok"}
        try:
            with urllib.request.urlopen(URL, timeout=45) as response:
                gpu = json.load(response)["devices"][0]
            total, free = gpu["vram_total"], gpu["vram_free"]
            row.update(used_gib=round((total-free)/2**30, 3),
                       free_gib=round(free/2**30, 3),
                       total_gib=round(total/2**30, 3),
                       utilization_pct=round(100*(total-free)/total, 2))
        except Exception as exc:
            row["status"] = str(exc)[:180]
        writer.writerow(row)
        f.flush()
        print(json.dumps(row, ensure_ascii=False), flush=True)
        time.sleep(2)
