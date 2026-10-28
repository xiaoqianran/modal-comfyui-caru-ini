from __future__ import annotations

import json
import os
from pathlib import Path

import modal

ROOT = Path(__file__).parent
WORKFLOW = os.getenv("WORKFLOW", "comfyui-pixal3d-trellis2")
# Only the local entrypoint reads the workspace config. Modal imports this
# module again in the remote container, where workflows/ is not mounted.
CONFIG = ROOT / "workflows" / WORKFLOW / "models.json"
# 与 comfyui.py 共用同一个模型缓存卷：Volumes v2，新卷名（v1 卷不能原地升级）。
volume = modal.Volume.from_name("modal-comfyui-models-v2", create_if_missing=True, version=2)
app = modal.App("modal-model-cache-download")
try:
    hf_secret = modal.Secret.from_name("huggingface-secret")
    hf_secret.hydrate()
except modal.exception.NotFoundError:
    hf_secret = modal.Secret.from_name("huggingface-s3")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("huggingface_hub>=0.30,<2", "hf_xet", "httpx")
    # 只开 Xet 高性能模式；HF_HUB_ENABLE_HF_TRANSFER 已废弃，不要再设置。
    .env({"HF_XET_HIGH_PERFORMANCE": "1"})
)

CACHE_DIR = "/cache"
SHARD_ROOT = "/cache/.download-shards"
MAX_SHARDS = 16


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be within {lo}..{hi}, got {value}")
    return value


def pack_shards(items: list[dict], target_bytes: int, max_shards: int) -> list[list[dict]]:
    """Greedy LPT bin packing: biggest file first, always into the least
    loaded shard, so shard totals end up as even as the file sizes allow."""
    if not items:
        return []
    total = sum(max(int(it.get("size") or 0), 0) for it in items)
    if target_bytes > 0 and total > 0:
        wanted = -(-total // target_bytes)  # ceil
    else:
        wanted = 1
    n = max(1, min(max_shards, wanted, len(items)))
    bins: list[list[dict]] = [[] for _ in range(n)]
    loads = [0] * n
    for it in sorted(items, key=lambda x: -(int(x.get("size") or 0))):
        i = min(range(n), key=lambda j: loads[j])
        bins[i].append(it)
        loads[i] += int(it.get("size") or 0)
    return [b for b in bins if b]


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=3600, secrets=[hf_secret], cpu=2.0, memory=4096)
def fetch_manifest(config_json: str) -> list[dict]:
    """Resolve every item's byte size: HF repo metadata in one API call per
    repo, a HEAD request for external URLs. Unknown sizes come back as 0."""
    import httpx
    from huggingface_hub import HfApi

    data = json.loads(config_json)
    token = os.environ.get("HF_TOKEN") or None
    repo_sizes: dict[str, dict[str, int]] = {}
    api = HfApi(token=token)
    for repo_id in sorted({m["repo_id"] for m in data.get("models", [])}):
        info = api.model_info(repo_id, files_metadata=True)
        repo_sizes[repo_id] = {
            s.rfilename: int(s.size or 0) for s in (info.siblings or []) if s.size
        }

    items: list[dict] = []
    for m in data.get("models", []):
        items.append({
            "kind": "hf",
            "repo_id": m["repo_id"],
            "filename": m["filename"],
            "size": repo_sizes.get(m["repo_id"], {}).get(m["filename"], 0),
        })
    for m in data.get("models_ext", []):
        size = 0
        try:
            with httpx.Client(follow_redirects=True, timeout=60) as client:
                head = client.head(m["url"])
                size = int(head.headers.get("content-length") or 0)
        except Exception as exc:  # size is only a packing hint
            print("WARN external size unknown:", m["url"], exc)
        items.append({"kind": "external", "url": m["url"], "filename": m["filename"], "size": size})
    return items


# 与 comfyui.py 构建期下载相同的快档资源：CPU=16 / MEMORY=32768 MiB。
@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=86400, secrets=[hf_secret], cpu=16.0, memory=32768)
def download_shard(payload: dict) -> dict:
    """One shard = one container. HF files go to this shard's private cache
    directory so parallel containers never race on the shared HF cache
    layout; external files land directly at their final path (unique names,
    atomic .part rename)."""
    import concurrent.futures
    import hashlib
    import time

    from huggingface_hub import hf_hub_download

    shard_id = int(payload["shard_id"])
    items = payload["items"]
    shard_cache = f"{SHARD_ROOT}/shard-{shard_id}"
    token = os.environ.get("HF_TOKEN") or None
    workers = _env_int("MODEL_DOWNLOAD_WORKERS", 4, 1, 8)
    started = time.time()
    errors: list[str] = []

    def fetch(item):
        kind = item["kind"]
        if kind == "hf":
            hf_hub_download(repo_id=item["repo_id"], filename=item["filename"],
                            cache_dir=shard_cache, token=token)
            print("CACHED HF", item["repo_id"], item["filename"], flush=True)
        else:
            target = Path(CACHE_DIR) / item["filename"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size:
                print("CACHED external", target.name, flush=True)
                return
            import httpx

            url = item["url"]
            temp = target.with_name(target.name + "." + hashlib.sha256(url.encode()).hexdigest()[:10] + ".part")
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
                    response.raise_for_status()
                    with temp.open("wb") as f:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            f.write(chunk)
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
            print("CACHED external", target.name, flush=True)

    def guarded(item):
        try:
            fetch(item)
        except Exception as exc:
            errors.append(f"{item['kind']}:{item.get('repo_id', '')}/{item['filename']}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items) or 1)) as pool:
        list(pool.map(guarded, items))

    if not errors:
        # Fail fast inside this shard if a "successful" download is not
        # actually resolvable from its own cache.
        for item in items:
            if item["kind"] == "hf":
                try:
                    hf_hub_download(repo_id=item["repo_id"], filename=item["filename"],
                                    cache_dir=shard_cache, token=token, local_files_only=True)
                except Exception as exc:
                    errors.append(f"verify {item['repo_id']}/{item['filename']}: {exc}")
    volume.commit()
    seconds = time.time() - started
    total = sum(int(it.get("size") or 0) for it in items)
    print(f"SHARD {shard_id} done: {len(items)} files, {total / 1e9:.2f} GB, "
          f"{seconds:.1f}s, {total / 1e6 / max(seconds, 0.1):.0f} MB/s, errors={len(errors)}", flush=True)
    return {"shard_id": shard_id, "ok": not errors, "errors": errors,
            "bytes": total, "seconds": seconds, "files": len(items)}


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=3600, secrets=[hf_secret], cpu=2.0, memory=8192)
def merge_shards(payload: dict) -> dict:
    """Fold every shard's private HF cache into the shared /cache layout,
    then verify every requested file resolves from the main cache and
    delete the shard directories."""
    import shutil

    from huggingface_hub import hf_hub_download

    volume.reload()
    items = payload["items"]
    shard_root = Path(SHARD_ROOT)
    main_cache = Path(CACHE_DIR)
    moved = 0

    def merge_tree(src_root: Path, dst_root: Path) -> None:
        # Generic recursive merge of a private HF cache into the shared one.
        # The layout is identical on both sides (repo dirs, content-addressed
        # blobs, the Xet CAS store under blobs/<2-hex>/, snapshot symlinks,
        # refs), so relative symlink targets stay valid verbatim. Identical
        # destinations are skipped; the shard copy is discarded.
        nonlocal moved
        for src in sorted(src_root.rglob("*")):
            rel = src.relative_to(src_root)
            dest = dst_root / rel
            if src.is_symlink():
                if dest.is_symlink() and os.readlink(dest) == os.readlink(src):
                    continue
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(os.readlink(src), dest)
                moved += 1
            elif src.is_file():
                if dest.exists():
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                moved += 1

    if shard_root.is_dir():
        for shard_dir in sorted(shard_root.glob("shard-*")):
            if shard_dir.is_dir():
                merge_tree(shard_dir, main_cache)
        shutil.rmtree(shard_root, ignore_errors=True)

    missing: list[str] = []
    token = os.environ.get("HF_TOKEN") or None
    for item in items:
        if item["kind"] == "hf":
            try:
                hf_hub_download(repo_id=item["repo_id"], filename=item["filename"],
                                cache_dir=CACHE_DIR, token=token, local_files_only=True)
            except Exception as exc:
                missing.append(f"{item['repo_id']}/{item['filename']}: {exc}")
        else:
            target = main_cache / item["filename"]
            if not (target.exists() and target.stat().st_size):
                missing.append(f"external {item['filename']} missing after merge")
    volume.commit()
    print(f"MERGE done: moved {moved} entries, missing={len(missing)}", flush=True)
    return {"ok": not missing, "missing": missing, "moved_entries": moved}


@app.local_entrypoint()
def main():
    import time

    if not CONFIG.is_file():
        raise ValueError(f"Missing workflow models.json: {CONFIG}")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config_json = json.dumps(config)
    print(f"Workflow: {WORKFLOW}")

    t0 = time.time()
    items = fetch_manifest.remote(config_json)
    total = sum(int(it.get("size") or 0) for it in items)
    target_gb = _env_int("MODEL_DOWNLOAD_SHARD_TARGET_GB", 64, 1, 1024)
    forced = _env_int("MODEL_DOWNLOAD_SHARDS", 0, 0, MAX_SHARDS)
    if forced:
        target = -(-total // forced) if total else 0
        shards = pack_shards(items, target, forced)
    else:
        shards = pack_shards(items, target_gb * 1_000_000_000, MAX_SHARDS)
    print(f"Models: {len(items)} files, {total / 1e9:.1f} GB total, {len(shards)} shard(s)")
    payloads = []
    for i, shard in enumerate(shards):
        shard_bytes = sum(int(it.get("size") or 0) for it in shard)
        print(f"  shard {i}: {len(shard)} files, {shard_bytes / 1e9:.1f} GB")
        payloads.append({"shard_id": i, "items": shard})

    results = list(download_shard.map(payloads))
    failed = [r for r in results if not r["ok"]]
    if failed:
        for r in failed:
            print(f"SHARD {r['shard_id']} FAILED:")
            for err in r["errors"]:
                print("   ", err)
        raise SystemExit(
            "Some shards failed; shard caches are kept on the volume, fix the "
            "cause and re-run — finished files resume instead of restarting.")

    merged = merge_shards.remote({"items": items})
    if not merged["ok"]:
        for m in merged["missing"]:
            print("MISSING after merge:", m)
        raise SystemExit("Merge verification failed; see lines above.")

    wall = time.time() - t0
    print(f"Downloaded: {len(items)} files, {total / 1e9:.1f} GB in {wall:.1f}s "
          f"({total / 1e6 / max(wall, 0.1):.0f} MB/s aggregate) across {len(shards)} container(s)")
    print("Model cache Volume: modal-comfyui-models-v2 (Volumes v2)")
