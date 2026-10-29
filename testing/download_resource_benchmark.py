
"""Hunyuan3D-2.1 完整权重下载评测：9 组 CPU / 内存配置。"""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import modal

# ===== 配置 =====
HF_TOKEN = os.environ.get("HF_TOKEN", "")
REPO = "tencent/Hunyuan3D-2.1"
WEIGHT_EXT = (".safetensors", ".ckpt", ".bin", ".pt", ".pth")

CPU_USD_PER_CORE_H = 0.0473
MEM_USD_PER_GIB_H = 0.008

app = modal.App("hunyuan3d-resource-benchmark")

image = modal.Image.debian_slim(
    python_version="3.11"
).pip_install(
    "huggingface_hub>=0.30,<2",
    "hf_xet",
    "psutil",
)

# 将内置 Token 传给 Modal 云端容器
download_secrets = [
    modal.Secret.from_dict({"HF_TOKEN": HF_TOKEN})
]


def measure(files, repo, workers, cpu, memory_gib):
    import concurrent.futures
    import tempfile
    import threading
    import time

    import psutil

    with tempfile.TemporaryDirectory(
        prefix="modal-fresh-bench-"
    ) as tmp:

        os.environ["HF_HOME"] = tmp
        os.environ["HF_HUB_CACHE"] = os.path.join(tmp, "hub")
        os.environ["HF_XET_CACHE"] = os.path.join(tmp, "xet")
        # Xet mode is set by Modal image environment before Python starts.

        from huggingface_hub import hf_hub_download

        token = os.environ["HF_TOKEN"]
        process = psutil.Process()
        stop = threading.Event()
        samples = []

        process.cpu_percent(None)

        def poll():
            while not stop.wait(0.5):
                try:
                    procs = [process] + process.children(
                        recursive=True
                    )
                    rss = sum(
                        p.memory_info().rss
                        for p in procs
                        if p.is_running()
                    )
                    cpu_percent = sum(
                        p.cpu_percent(None)
                        for p in procs
                        if p.is_running()
                    )
                    samples.append((rss, cpu_percent))
                except (psutil.Error, OSError):
                    pass

        def fetch(info):
            path = hf_hub_download(
                repo_id=repo,
                filename=info["path"],
                revision=info["revision"],
                cache_dir=os.path.join(tmp, "hub"),
                force_download=True,
                token=token,
            )

            actual = os.path.getsize(path)

            if actual != info["size"]:
                raise RuntimeError(
                    f"Size mismatch: {info['path']}"
                )

            return actual

        started = time.perf_counter()
        cpu_start = time.process_time()

        monitor = threading.Thread(
            target=poll,
            daemon=True,
        )
        monitor.start()

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers
            ) as executor:
                sizes = list(executor.map(fetch, files))
        finally:
            stop.set()
            monitor.join(timeout=2)

        elapsed = time.perf_counter() - started
        bytes_total = sum(sizes)

        price_per_hour = (
            cpu * CPU_USD_PER_CORE_H
            + memory_gib * MEM_USD_PER_GIB_H
        )
        estimated_cost = price_per_hour * elapsed / 3600

        peak_rss = max(
            [s[0] for s in samples]
            + [process.memory_info().rss]
        )

        return {
            "cpu": cpu,
            "memory_gib": memory_gib,
            "threads": workers,
            "files": len(files),
            "download_gib": round(
                bytes_total / 1024**3, 4
            ),
            "elapsed_s": round(elapsed, 3),
            "throughput_mib_s": round(
                bytes_total / 1048576 / max(elapsed, 1e-6),
                2,
            ),
            "process_cpu_s": round(
                time.process_time() - cpu_start, 2
            ),
            "cpu_mean_cores_sampled": (
                round(
                    sum(s[1] for s in samples)
                    / len(samples)
                    / 100,
                    2,
                )
                if samples else None
            ),
            "peak_process_rss_gib": round(
                peak_rss / 1024**3, 3
            ),
            "price_per_hour_usd": round(
                price_per_hour, 5
            ),
            "estimated_cpu_memory_usd": round(
                estimated_cost, 6
            ),
            "estimated_usd_per_gib": round(
                estimated_cost / (bytes_total / 1024**3),
                6,
            ),
        }


# A/B: separate Modal image environments to set Xet before process startup.
@app.function(image=image.env({"HF_XET_HIGH_PERFORMANCE": "1"}),
              cpu=4, memory=4096, timeout=3600, secrets=download_secrets)
def xet_on(files, repo):
    result = measure(files, repo, 2, 4, 4)
    return {"xet_high_performance": "ON", **result}


@app.function(image=image.env({"HF_XET_HIGH_PERFORMANCE": "0"}),
              cpu=4, memory=4096, timeout=3600, secrets=download_secrets)
def xet_off(files, repo):
    result = measure(files, repo, 2, 4, 4)
    return {"xet_high_performance": "OFF", **result}


# ===== 9 组 CPU / 内存配置 =====

@app.function(image=image, cpu=2, memory=4096,
              timeout=3600, secrets=download_secrets)
def c2_m4(files, repo, threads):
    return measure(files, repo, threads, 2, 4)


@app.function(image=image, cpu=2, memory=8192,
              timeout=3600, secrets=download_secrets)
def c2_m8(files, repo, threads):
    return measure(files, repo, threads, 2, 8)


@app.function(image=image, cpu=2, memory=16384,
              timeout=3600, secrets=download_secrets)
def c2_m16(files, repo, threads):
    return measure(files, repo, threads, 2, 16)


@app.function(image=image, cpu=4, memory=4096,
              timeout=3600, secrets=download_secrets)
def c4_m4(files, repo, threads):
    return measure(files, repo, threads, 4, 4)


@app.function(image=image, cpu=4, memory=8192,
              timeout=3600, secrets=download_secrets)
def c4_m8(files, repo, threads):
    return measure(files, repo, threads, 4, 8)


@app.function(image=image, cpu=4, memory=16384,
              timeout=3600, secrets=download_secrets)
def c4_m16(files, repo, threads):
    return measure(files, repo, threads, 4, 16)


@app.function(image=image, cpu=8, memory=4096,
              timeout=3600, secrets=download_secrets)
def c8_m4(files, repo, threads):
    return measure(files, repo, threads, 8, 4)


@app.function(image=image, cpu=8, memory=8192,
              timeout=3600, secrets=download_secrets)
def c8_m8(files, repo, threads):
    return measure(files, repo, threads, 8, 8)


@app.function(image=image, cpu=8, memory=16384,
              timeout=3600, secrets=download_secrets)
def c8_m16(files, repo, threads):
    return measure(files, repo, threads, 8, 16)


RESOURCE_CASES = [
    ("2CPU/4GiB", c2_m4),
    ("2CPU/8GiB", c2_m8),
    ("2CPU/16GiB", c2_m16),
    ("4CPU/4GiB", c4_m4),
    ("4CPU/8GiB", c4_m8),
    ("4CPU/16GiB", c4_m16),
    ("8CPU/4GiB", c8_m4),
    ("8CPU/8GiB", c8_m8),
    ("8CPU/16GiB", c8_m16),
]


@app.local_entrypoint()
def main():
    from huggingface_hub import HfApi

    if not HF_TOKEN.startswith("hf_") or "在这里" in HF_TOKEN:
        raise ValueError("请先在文件顶部填写 HF_TOKEN")

    repo = os.getenv("BENCH_REPO", REPO)
    workers = int(os.getenv("BENCH_THREADS", "8"))

    if not 1 <= workers <= 32:
        raise ValueError("BENCH_THREADS must be 1..32")

    api = HfApi(token=HF_TOKEN)

    revision = api.repo_info(repo_id=repo).sha

    entries = [
        {
            "path": f.path,
            "size": f.size,
            "revision": revision,
        }
        for f in api.list_repo_tree(
            repo,
            revision=revision,
            recursive=True,
            expand=True,
        )
        if f.path.lower().endswith(WEIGHT_EXT)
        and isinstance(getattr(f, "size", None), int)
        and f.size > 0
    ]

    if not entries:
        raise RuntimeError("No model weights found")

    selected = sorted(entries, key=lambda e: e["path"])
    total = sum(e["size"] for e in selected)

    print(
        f"repo={repo} revision={revision} "
        f"files={len(selected)} "
        f"size={total / 1024**3:.3f} GiB/case",
        flush=True,
    )

    mode = os.getenv("BENCH_MODE", "resources").strip().lower()
    if mode == "xet":
        import statistics
        repeats = int(os.getenv("BENCH_REPEATS", "5"))
        if not 1 <= repeats <= 10:
            raise ValueError("BENCH_REPEATS must be 1..10")
        # ABBA/BAAB... paired alternation, with independent fresh caches.
        pairs = [("OFF", xet_off), ("ON", xet_on)]
        cases = [(i, label, fn) for i in range(1, repeats + 1)
                 for label, fn in (pairs if i % 2 else pairs[::-1])]
        output_prefix = "xet_benchmark_"
    elif mode == "threads":
        import statistics
        repeats = int(os.getenv("BENCH_REPEATS", "3"))
        if not 1 <= repeats <= 10:
            raise ValueError("BENCH_REPEATS must be 1..10")
        thread_counts = [2, 4, 6, 8]
        # Reverse alternating rounds to reduce fixed-order network bias.
        cases = [(round_no, n) for round_no in range(1, repeats + 1)
                 for n in (thread_counts if round_no % 2 else thread_counts[::-1])]
        output_prefix = "thread_benchmark_"
    elif mode == "resources":
        cases = [(None, name, fn, workers) for name, fn in RESOURCE_CASES]
        output_prefix = "resource_benchmark_"
    else:
        raise ValueError("BENCH_MODE must be resources, threads or xet")

    output = Path("testing") / (
        output_prefix + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".csv"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for case in cases:
        if mode == "xet":
            round_no, label, fn = case
            print(f"START round={round_no}/{repeats} XET={label} 4CPU/4GiB/2threads", flush=True)
            result = fn.remote(selected, repo)
        elif mode == "threads":
            round_no, n = case
            name, fn, nworkers = "4CPU/4GiB", c4_m4, n
            print(f"START round={round_no}/{repeats} {name}/{nworkers} threads", flush=True)
            result = fn.remote(selected, repo, nworkers)
        else:
            round_no, name, fn, nworkers = case
            print(f"START {name}/{nworkers}", flush=True)
            result = fn.remote(selected, repo, nworkers)
        if round_no is not None:
            result = {"round": round_no, **result}
        results.append(result)
        print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
        # Persist every completed run so partial results survive a later failure.
        with output.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
        print("RESULT CSV:", output, flush=True)

    if mode == "xet":
        for label in ("OFF", "ON"):
            times = [r["elapsed_s"] for r in results if r["xet_high_performance"] == label]
            print(f"SUMMARY XET={label} runs={len(times)} median_s={statistics.median(times):.3f} "
                  f"min_s={min(times):.3f} max_s={max(times):.3f}", flush=True)
    if mode == "threads":
        for n in thread_counts:
            times = [r["elapsed_s"] for r in results if r["threads"] == n]
            print(f"SUMMARY threads={n} runs={len(times)} median_s={statistics.median(times):.3f} "
                  f"min_s={min(times):.3f} max_s={max(times):.3f}", flush=True)
    print("Costs are estimates, not official billing.", flush=True)
