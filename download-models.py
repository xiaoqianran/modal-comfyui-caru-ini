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
    # 关闭 Xet 高性能模式（基准测试确定的默认值）。
    .env({"HF_XET_HIGH_PERFORMANCE": "0"})
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


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)


def _safe_path(root: Path, name: str) -> Path:
    from pathlib import PurePosixPath
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ValueError(f"Unsafe cache path: {name!r}")
    return root.joinpath(*path.parts)


def _cached_path(root: Path, item: dict) -> Path:
    if item["kind"] == "external":
        return _safe_path(root, item["filename"])
    repo = _safe_path(root, "models--" + item["repo_id"].replace("/", "--"))
    return _safe_path(repo / "snapshots" / item["revision"], item["filename"])


def _size_valid(path: Path, item: dict) -> bool:
    # Permission/IO errors must propagate; only missing files are ordinary misses.
    import stat
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    expected = item.get("size")
    return stat.S_ISREG(info.st_mode) and info.st_size > 0 and (expected is None or info.st_size == expected)


def _verify_content(path: Path, item: dict) -> str:
    import hashlib
    if not _size_valid(path, item):
        raise ValueError(f"Missing file or size mismatch: {path}")
    algorithm = item.get("algorithm", "sha256")
    digest = hashlib.new(algorithm)
    if algorithm == "sha1":
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    if item.get("digest") and value != item["digest"]:
        raise ValueError(f"Content hash mismatch: {path}")
    return value


def _stage_dir(run_root: Path, item: dict) -> Path:
    import hashlib
    # Stable across cache hits, changed packing, worker counts and retries.
    key = json.dumps(item, sort_keys=True)
    return run_root / "files" / hashlib.sha256(key.encode()).hexdigest()


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=3600, secrets=[hf_secret], cpu=2.0, memory=4096)
def fetch_manifest(config_json: str, workflow: str = WORKFLOW, update: bool = False) -> list[dict]:
    """Resolve pinned metadata once. One complete download job at a time."""
    import re
    from huggingface_hub import HfApi
    volume.reload()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", workflow):
        raise ValueError("Invalid workflow name")
    root = Path(CACHE_DIR)
    pins = root / ".workflow-pins" / f"{workflow}.json"
    success = root / ".workflow-revisions" / f"{workflow}.json"
    old_file = pins if pins.exists() else success
    old = json.loads(old_file.read_text(encoding="utf-8")) if old_file.exists() else {}
    data = json.loads(config_json)
    api = HfApi(token=os.getenv("HF_TOKEN") or None)
    repos, items = {}, []
    for model in data.get("models", []):
        repo, filename = model["repo_id"], model["filename"]
        revision = model.get("revision") or (None if update else old.get(f"{repo}/{filename}")) or "main"
        if (repo, revision) not in repos:
            repos[repo, revision] = api.model_info(repo, revision=revision, files_metadata=True)
        info = repos[repo, revision]
        entry = next((e for e in info.siblings or [] if e.rfilename == filename), None)
        if entry is None or entry.size is None:
            raise ValueError(f"Missing HF file metadata: {repo}/{filename}")
        lfs = entry.lfs
        digest = lfs.sha256 if lfs else entry.blob_id
        if not digest or not re.fullmatch(r"[0-9a-f]{64}" if lfs else r"[0-9a-f]{40}", digest):
            raise ValueError(f"Missing HF content hash: {repo}/{filename}")
        items.append(dict(kind="hf", repo_id=repo, filename=filename, revision=info.sha,
                          size=entry.size, algorithm="sha256" if lfs else "sha1", digest=digest))
    for model in data.get("models_ext", []):
        import httpx
        size = model.get("size")
        if size is None:
            try:
                with httpx.Client(follow_redirects=True, timeout=60) as client:
                    response = client.head(model["url"], headers={"Accept-Encoding": "identity"})
                    response.raise_for_status()
                    size = int(response.headers["content-length"]) if "content-length" in response.headers else None
            except (httpx.HTTPError, ValueError) as exc:
                print("WARN external size unknown:", model["url"], exc)
        digest = model.get("sha256")
        if digest and not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("Invalid external sha256")
        if not digest:
            print("WARN no upstream SHA256; external integrity limited to size/transport:", model["filename"])
        items.append(dict(kind="external", url=model["url"], filename=model["filename"],
                          size=size, algorithm="sha256", digest=digest.lower() if digest else None))
    # Validate/deduplicate before packing so no two workers write the same staged file.
    unique = {}
    for item in items:
        if item["kind"] == "external" and ("/" in item["filename"] or item["filename"].startswith(".")):
            raise ValueError("External filename must be a plain filename")
        target = str(_cached_path(root, item))
        if target in unique and unique[target] != item:
            raise ValueError(f"Conflicting models: {target}")
        unique[target] = item
    items = list(unique.values())
    # Pending pins persist before download; readers still use last successful record.
    _atomic_json(pins, {f"{i['repo_id']}/{i['filename']}": i["revision"] for i in items if i["kind"] == "hf"})
    volume.commit()
    return items


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=3600, cpu=2.0, memory=4096)
def check_cache(items: list[dict]) -> list[dict]:
    volume.reload()
    missing = []
    for item in items:
        hit = _size_valid(_cached_path(Path(CACHE_DIR), item), item)
        print("CACHE HIT" if hit else "CACHE MISS", item["filename"], flush=True)
        if not hit:
            missing.append(item)
    return missing


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=300, cpu=1.0, memory=1024)
def quick_check(config_json: str, workflow: str) -> list[str]:
    """Fast cache check using the last successful revision record, without hashing weights."""
    volume.reload()
    root = Path(CACHE_DIR)
    config = json.loads(config_json)
    record = root / ".workflow-revisions" / f"{workflow}.json"
    revisions = json.loads(record.read_text(encoding="utf-8")) if record.is_file() else {}
    missing = []
    for model in config.get("models", []):
        key = f"{model['repo_id']}/{model['filename']}"
        revision = revisions.get(key)
        pinned = model.get("revision")
        if not revision or (pinned and len(pinned) == 40 and pinned != revision):
            missing.append(key)
            continue
        item = {"kind": "hf", "repo_id": model["repo_id"], "filename": model["filename"],
                "revision": revision}
        if not _size_valid(_cached_path(root, item), item):
            missing.append(key)
    for model in config.get("models_ext", []):
        item = {"kind": "external", "filename": model["filename"], "size": model.get("size")}
        if not _size_valid(_cached_path(root, item), item):
            missing.append(model["filename"])
    return missing


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=86400, secrets=[hf_secret], cpu=4.0, memory=4096)
def download_shard(payload: dict) -> dict:
    """Parallel containers only write disjoint per-file staging, never final cache."""
    import concurrent.futures
    import time
    from huggingface_hub import hf_hub_download
    volume.reload()
    started = time.time()
    items = payload["items"]
    run_root = Path(SHARD_ROOT) / payload["run_id"]
    workers = max(1, min(8, int(payload.get("workers") or 2)))
    errors = []

    def fetch(item):
        stage = _stage_dir(run_root, item)
        target = _cached_path(stage, item)
        if item["kind"] == "hf":
            # Public content-addressed blobs may already exist under another revision.
            public_blob = Path(CACHE_DIR) / ("models--" + item["repo_id"].replace("/", "--")) / "blobs" / item["digest"]
            candidate = target if _size_valid(target, item) else public_blob
            if _size_valid(candidate, item):
                try:
                    _verify_content(candidate, item)
                    if candidate != target:
                        # Reuse the public blob without moving or duplicating it.
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temp = target.with_name(target.name + ".publish")
                        temp.unlink(missing_ok=True)
                        temp.symlink_to(os.path.relpath(candidate, target.parent))
                        os.replace(temp, target)
                    return
                except ValueError:
                    pass  # Corrupt complete cache: force a fresh native download.
            force = target.exists()
            for attempt in range(2):
                cached = hf_hub_download(repo_id=item["repo_id"], filename=item["filename"],
                    revision=item["revision"], cache_dir=str(stage), token=os.getenv("HF_TOKEN") or None,
                    force_download=force or attempt > 0)
                try:
                    _verify_content(Path(cached), item)
                    return
                except ValueError:
                    if attempt:
                        raise
        else:
            if _size_valid(target, item):
                try:
                    _verify_content(target, item)
                    return
                except ValueError:
                    pass
            import httpx
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            with httpx.stream("GET", item["url"], follow_redirects=True, timeout=120,
                              headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                with part.open("wb") as stream:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        stream.write(chunk)
            _verify_content(part, item)
            os.replace(part, target)

    def guarded(item):
        try:
            fetch(item)
        except Exception as exc:
            errors.append(f"{item['kind']}:{item['filename']}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items) or 1)) as pool:
        list(pool.map(guarded, items))
    volume.commit()  # Completed files and HF .incomplete files remain resumable.
    return dict(shard_id=payload["shard_id"], ok=not errors, errors=errors,
                seconds=time.time() - started, files=len(items))


def _publish(source: Path, root: Path, item: dict) -> None:
    """Publish verified staging with same-volume rename and restart recovery."""
    target = _cached_path(root, item)
    blob = (root / ("models--" + item["repo_id"].replace("/", "--")) / "blobs" / item["digest"]
            if item["kind"] == "hf" else target)
    if not _size_valid(blob, item):
        if not _size_valid(source, item):
            raise ValueError(f"Missing staged and published file: {source}")
        blob.parent.mkdir(parents=True, exist_ok=True)
        # Never move a shared cache blob: reused public blobs are already valid.
        actual = source.resolve() if item["kind"] == "hf" else source
        public_root = (Path(CACHE_DIR) / ("models--" + item["repo_id"].replace("/", "--")) / "blobs"
                       if item["kind"] == "hf" else None)
        if public_root is not None and actual.parent == public_root:
            if actual != blob:
                raise ValueError(f"Unexpected public blob source: {actual}")
        else:
            os.replace(actual, blob)
    if not _size_valid(blob, item):
        raise ValueError(f"Invalid published file: {blob}")
    if item["kind"] == "hf":
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".publish")
        temp.unlink(missing_ok=True)
        temp.symlink_to(os.path.relpath(blob, target.parent))
        os.replace(temp, target)


@app.function(image=image, volumes={CACHE_DIR: volume}, timeout=86400, secrets=[hf_secret], cpu=2.0, memory=8192)
def merge_shards(payload: dict) -> dict:
    import shutil
    import time
    started = time.monotonic()
    print("MERGE reload: refreshing Volume", flush=True)
    volume.reload()
    def log(stage: str, since: float) -> None:
        print(f"MERGE {stage}: {time.monotonic() - since:.2f}s "
              f"(total {time.monotonic() - started:.2f}s)", flush=True)
    log("reload", started)
    root = Path(CACHE_DIR)
    run_root = Path(SHARD_ROOT) / payload["run_id"]
    # Only requested missing files are published. Every source was hash-verified
    # by its shard; merge does cheap size checks, not another full hash pass.
    pending = payload.get("downloaded_items", [])
    phase = time.monotonic()
    print(f"MERGE publish: {len(pending)} files (same-volume rename)", flush=True)
    for item in pending:
        source = _cached_path(_stage_dir(run_root, item), item)
        _publish(source, root, item)
    log("publish", phase)
    missing = [i["filename"] for i in payload["items"] if not _size_valid(_cached_path(root, i), i)]
    if missing:
        return dict(ok=False, missing=missing, moved_entries=len(pending))
    revisions = {f"{i['repo_id']}/{i['filename']}": i["revision"] for i in payload["items"] if i["kind"] == "hf"}
    phase = time.monotonic()
    print("MERGE commit: publishing model files", flush=True)
    volume.commit()
    log("commit", phase)
    # The second commit publishes the success record and optional cleanup.
    # If it fails, the first commit still preserves all model data for retry.
    _atomic_json(root / ".workflow-revisions" / f"{payload['workflow']}.json", revisions)
    if run_root.is_dir():
        phase = time.monotonic()
        print("MERGE cleanup: removing staged files", flush=True)
        try:
            shutil.rmtree(run_root)
            log("cleanup", phase)
        except Exception as exc:
            print(f"WARN MERGE cleanup deferred: {exc}", flush=True)
    phase = time.monotonic()
    print("MERGE final commit: success record and cleanup", flush=True)
    volume.commit()
    log("final commit", phase)
    return dict(ok=True, missing=[], moved_entries=len(pending))


@app.local_entrypoint()
def main(update: bool = False, check_only: bool = False, ensure: bool = False):
    import hashlib
    import time

    if not CONFIG.is_file():
        raise ValueError(f"Missing workflow models.json: {CONFIG}")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    config_json = json.dumps(config)
    if check_only or ensure:
        missing = quick_check.remote(config_json, WORKFLOW)
        if missing:
            print(f"CACHE INCOMPLETE: {len(missing)} missing model(s):", flush=True)
            for name in missing:
                print(f"  {name}", flush=True)
            if check_only:
                raise SystemExit(3)
        else:
            print(f"CACHE READY: {WORKFLOW} ({len(config.get('models', [])) + len(config.get('models_ext', []))} models)", flush=True)
            return
    print(f"Workflow: {WORKFLOW} (one complete download job at a time)")

    t0 = time.time()
    items = fetch_manifest.remote(config_json, WORKFLOW, update)
    # Same manifest (files + pinned revisions) -> same run id, so a re-run
    # resumes into the same shard dirs; a different manifest is isolated.
    fingerprint = json.dumps(items, sort_keys=True)
    run_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
    print(f"Run id: {run_id}")
    missing_items = check_cache.remote(items)
    workers = _env_int("MODEL_DOWNLOAD_WORKERS", 2, 1, 8)
    total = sum(int(it.get("size") or 0) for it in missing_items)
    print(f"Cache: {len(items) - len(missing_items)} hit(s), {len(missing_items)} missing")
    target_gb = _env_int("MODEL_DOWNLOAD_SHARD_TARGET_GB", 16, 1, 1024)
    forced = _env_int("MODEL_DOWNLOAD_SHARDS", 0, 0, MAX_SHARDS)
    if forced:
        target = -(-total // forced) if total else 0
        shards = pack_shards(missing_items, target, forced)
    else:
        shards = pack_shards(missing_items, target_gb * 1_000_000_000, MAX_SHARDS)
    print(f"Models: {len(items)} requested, {len(missing_items)} to download, {total / 1e9:.1f} GB, {len(shards)} shard(s)")
    payloads = []
    for i, shard in enumerate(shards):
        shard_bytes = sum(int(it.get("size") or 0) for it in shard)
        print(f"  shard {i}: {len(shard)} files, {shard_bytes / 1e9:.1f} GB")
        payloads.append({"shard_id": i, "items": shard, "run_id": run_id, "workers": workers})

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

    merged = merge_shards.remote({"items": items, "run_id": run_id, "workflow": WORKFLOW,
                                  "downloaded_items": missing_items})
    if not merged["ok"]:
        for m in merged["missing"]:
            print("MISSING after merge:", m)
        raise SystemExit("Merge verification failed; see lines above.")

    wall = time.time() - t0
    print(f"Downloaded: {len(missing_items)} files, {total / 1e9:.1f} GB in {wall:.1f}s "
          f"({total / 1e6 / max(wall, 0.1):.0f} MB/s aggregate) across {len(shards)} container(s)")
    print("Model cache Volume: modal-comfyui-models-v2 (Volumes v2)")
