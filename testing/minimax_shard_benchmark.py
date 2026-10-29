"""Compare 16/32 GB Modal sharding on pinned MiniMax M3.1 Flash weights.
Usage: uv run modal run --detach testing/minimax_shard_benchmark.py::run_benchmark --target-gb 16 --run-label v2-16-a
       uv run modal run --detach testing/minimax_shard_benchmark.py::run_benchmark --target-gb 32
Data lives in a separate benchmark Volume, never the production model cache.
"""
from __future__ import annotations
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import modal

REPO = "MiniMaxAI/MiniMax-M3.1-Flash"
REVISION = "262ae6830934f3512211081b8a3f16425b93a239"
MAX_SHARDS = 16
# All new benchmark storage uses Volume v2. Do not reuse the old v1 name.
VOLUME = modal.Volume.from_name("minimax-m31-shard-benchmark-v2", create_if_missing=True, version=2)
IMAGE = (modal.Image.debian_slim(python_version="3.11")
         .pip_install("huggingface_hub>=0.30,<2", "hf_xet")
         .env({"HF_XET_HIGH_PERFORMANCE": "0"}))
APP = modal.App("minimax-m31-shard-benchmark")
# Reuse the existing cloud-side HF Token secret; never store it in source.
HF_SECRET = modal.Secret.from_name("huggingface-secret")


def pack(items, target_gb):
    count = min(MAX_SHARDS, len(items), math.ceil(sum(x["size"] for x in items) / (target_gb * 1e9)))
    bins = [[] for _ in range(count)]
    loads = [0] * count
    for item in sorted(items, key=lambda x: -x["size"]):
        i = min(range(count), key=lambda j: loads[j])
        bins[i].append(item)
        loads[i] += item["size"]
    return bins


@APP.function(image=IMAGE, cpu=4, memory=4096, timeout=14400,
              volumes={"/bench": VOLUME}, max_containers=16, secrets=[HF_SECRET])
def worker(target_gb: int, shard_id: int, items: list[dict], run_label: str):
    from concurrent.futures import ThreadPoolExecutor
    from huggingface_hub import hf_hub_download
    started = time.monotonic()
    cache = f"/bench/runs/{run_label}/{target_gb}gb/shard-{shard_id}/hub"
    os.environ["HF_XET_CACHE"] = f"/bench/runs/{run_label}/{target_gb}gb/shard-{shard_id}/xet"
    def fetch(item):
        try:
            # Verify actual cache state before measuring. A successful HF call
            # is not necessarily a new network download.
            hit = False
            try:
                hf_hub_download(repo_id=REPO, filename=item["filename"],
                                revision=REVISION, cache_dir=cache,
                                local_files_only=True)
                hit = True
            except Exception:
                pass
            path = hf_hub_download(repo_id=REPO, filename=item["filename"],
                                   revision=REVISION, cache_dir=cache)
            actual = Path(path).stat().st_size
            if actual != item["size"]:
                raise RuntimeError(f"size mismatch expected={item['size']} actual={actual}")
            return {"cached": hit, "size": actual, "error": None}
        except Exception as exc:
            return {"cached": False, "size": 0,
                    "error": f"{item['filename']}: {exc}"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        downloaded = list(pool.map(fetch, items))
    errors = [x["error"] for x in downloaded if x["error"]]
    cache_hits = sum(x["cached"] for x in downloaded)
    new_bytes = sum(x["size"] for x in downloaded if not x["cached"])
    VOLUME.commit()
    duration = time.monotonic() - started
    result = {"target_gb": target_gb, "shard_id": shard_id, "files": len(items),
              "bytes": sum(x["size"] for x in items), "seconds": round(duration, 2),
              "errors": errors, "cache_hits": cache_hits, "new_logical_bytes": new_bytes}
    print("SHARD_RESULT", json.dumps(result), flush=True)
    return result


@APP.function(image=IMAGE, cpu=1, memory=2048, timeout=21600,
              volumes={"/bench": VOLUME}, secrets=[HF_SECRET])
def run_benchmark(target_gb: int = 16, run_label: str = "hf-token-test"):
    """All manifest lookup, scheduling, download, and results happen in Modal."""
    if target_gb not in (16, 32):
        raise ValueError("target_gb must be 16 or 32")
    if not run_label or not all(c.isalnum() or c in "-_" for c in run_label):
        raise ValueError("run_label may contain only letters, digits, dashes and underscores")
    if not os.environ.get("HF_TOKEN"):
        raise ValueError("HF_TOKEN missing from Modal secret huggingface-secret")
    from huggingface_hub import HfApi
    info = HfApi().model_info(REPO, revision=REVISION, files_metadata=True)
    items = [{"filename": f.rfilename, "size": int(f.size)} for f in info.siblings
             if f.rfilename.endswith(".safetensors") and f.size]
    groups = pack(items, target_gb)
    total_bytes = sum(x["size"] for x in items)
    print(f"BENCH_START target={target_gb}GB files={len(items)} shards={len(groups)} total_gb={total_bytes/1e9:.3f}", flush=True)
    started = time.monotonic()
    results = list(worker.map([target_gb] * len(groups), range(len(groups)), groups, [run_label] * len(groups)))
    elapsed = time.monotonic() - started
    failures = [e for r in results for e in r["errors"]]
    new_logical_bytes = sum(r["new_logical_bytes"] for r in results)
    cache_hits = sum(r["cache_hits"] for r in results)
    summary = {"target_gb": target_gb, "revision": REVISION, "files": len(items),
               "shards": len(groups), "bytes": total_bytes, "run_label": run_label,
               "wall_seconds": round(elapsed, 2),
               "effective_mb_s": (round(new_logical_bytes / 1e6 / elapsed, 2) if cache_hits == 0 and not failures else None),
               "cache_hits": cache_hits, "new_logical_bytes": new_logical_bytes,
               "valid_cold_benchmark": cache_hits == 0 and not failures,
               "failures": len(failures), "timestamp": datetime.now(timezone.utc).isoformat()}
    report_dir = Path("/bench/results")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{run_label}-{target_gb}gb.json"
    report_path.write_text(json.dumps({"summary": summary, "shards": results}, indent=2), encoding="utf-8")
    VOLUME.commit()
    print("BENCH_SUMMARY", json.dumps(summary), flush=True)
    print("RESULT_PATH", report_path, flush=True)
    if failures:
        raise RuntimeError(f"{len(failures)} files failed: {failures[:3]}")
    return summary
