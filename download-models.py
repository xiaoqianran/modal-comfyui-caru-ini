from __future__ import annotations

import json
import os
from pathlib import Path

import modal

ROOT = Path(__file__).parent
WORKFLOW = os.getenv("WORKFLOW", "comfyui-pixal3d-trellis2")
# Only the local entrypoint reads the workspace config. Modal imports this
# module again in the remote container, where workflows/ is not mounted.
CONFIG = Path(os.getenv("MODEL_CONFIG_PATH") or ROOT / "workflows" / WORKFLOW / "models.json")
# 同名 Volume 供下载与推理共用；默认保持现有缓存，试点时可显式切换。
MODEL_VOLUME_NAME = os.getenv("MODEL_VOLUME_NAME", "model-store")
volume = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=True, version=2)
app = modal.App("modal-model-cache-download")
try:
    hf_secret_name = "huggingface-secret"
    hf_secret = modal.Secret.from_name("huggingface-secret")
    hf_secret.hydrate()
except modal.exception.NotFoundError:
    hf_secret_name = "huggingface-s3"
    hf_secret = modal.Secret.from_name("huggingface-s3")
image = (
    modal.Image.debian_slim(python_version="3.11")
    .uv_pip_install("huggingface_hub>=0.30,<2", "hf_xet", "httpx")
    # 关闭 Xet 高性能模式（基准测试确定的默认值）。
    .env({"HF_XET_HIGH_PERFORMANCE": "0",
          "MODEL_VOLUME_NAME": MODEL_VOLUME_NAME, "WORKFLOW": WORKFLOW})
)

CACHE_DIR = "/data/hf"
SHARD_ROOT = CACHE_DIR + "/.download-shards"
MAX_SHARDS = 16

def benchmark_manifest(items: list[dict]) -> dict:
    grouped = {}
    for item in items:
        if item["kind"] != "hf":
            continue
        key = (item["repo_id"], item["revision"])
        files = grouped.setdefault(key, {})
        entry = {"size": item["size"],
                 "sha256": item["digest"] if item["algorithm"] == "sha256" else None}
        if item["filename"] in files and files[item["filename"]] != entry:
            raise ValueError(f"Conflicting benchmark file: {key}/{item['filename']}")
        files[item["filename"]] = entry
    return {"version": 1, "models": [
        {"repo_id": repo, "revision": revision, "files": files}
        for (repo, revision), files in grouped.items()
    ]}


def benchmark_sources(items: list[dict]) -> list[dict]:
    """将精确文件清单映射为 benchmark 0.4.x 的下载参数。"""
    sources = []
    seen = {}
    for model in benchmark_manifest(items)["models"]:
        repo, revision = model["repo_id"], model["revision"]
        if repo in seen and seen[repo] != revision:
            raise ValueError(f"One download cannot mix revisions of {repo}")
        seen[repo] = revision
        filenames = list(model["files"])
        # benchmark 的 include 使用 fnmatch 和逗号分隔，不支持转义通配符。
        if any(name != name.strip() or any(char in name for char in ",*?[]")
               for name in filenames):
            raise ValueError(f"Unsupported benchmark filename filter in {repo}")
        sources.append({"repo_id": repo, "revision": revision,
                        "include": ",".join(filenames)})
    return sources


def download_hf_items(items: list[dict], base: str, downloader, *, workers: int) -> dict:
    """只有真实下载结果与计划的每个文件完全匹配时才允许后续发布。"""
    expected = benchmark_manifest(items)
    result = downloader.download(benchmark_sources(items), base=base, workers=workers)
    if (result.get("status") != "VERIFIED"
            or result.get("verification_mode") != "xet"
            or result.get("base") != base
            or result.get("manifest") != expected):
        raise RuntimeError("benchmark result differs from pinned ComfyUI manifest")
    return result


def benchmark_path(base: Path, item: dict) -> Path:
    root = _safe_path(base, item["repo_id"])
    return _safe_path(root / item["revision"] / "model", item["filename"])


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


def hf_download_workers() -> int:
    """Number of HF Writer containers; independent of external URL threads."""
    return _env_int("MODEL_HF_DOWNLOAD_WORKERS", 3, 1, 8)


def create_hf_downloader(workers: int):
    """Create the independent benchmark App only when HF files are missing."""
    from modal_benchmark.hf import create_downloader

    return create_downloader(
        volume_name=MODEL_VOLUME_NAME, volume_version=2,
        mount_path="/data", sub_path="/comfyui", secret_name=hf_secret_name,
        verification_mode="xet", max_workers=workers,
    )


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


def _verify_content(path: Path, item: dict) -> None:
    import hashlib
    if not _size_valid(path, item):
        raise ValueError(f"Missing file or size mismatch: {path}")
    if not item.get("digest"):
        return
    algorithm = item.get("algorithm", "sha256")
    digest = hashlib.new(algorithm)
    if algorithm == "sha1":
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != item["digest"]:
        raise ValueError(f"Content hash mismatch: {path}")


def _stage_dir(run_root: Path, item: dict) -> Path:
    import hashlib
    # Stable across cache hits, changed packing, worker counts and retries.
    key = json.dumps(item, sort_keys=True)
    return run_root / "files" / hashlib.sha256(key.encode()).hexdigest()


@app.function(image=image, serialized=False, volumes={"/data": volume.with_mount_options(sub_path="/comfyui")}, timeout=3600, secrets=[hf_secret], cpu=2.0, memory=4096)
def fetch_manifest(config_json: str, workflow: str = WORKFLOW, update: bool = False) -> dict:
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
    revisions = {f"{i['repo_id']}/{i['filename']}": i["revision"] for i in items if i["kind"] == "hf"}
    # Persist changed pins before downloading. The success record is published separately.
    if not pins.exists() or old != revisions:
        _atomic_json(pins, revisions)
        volume.commit()
    published = json.loads(success.read_text(encoding="utf-8")) if success.is_file() else None
    missing = []
    for item in items:
        hit = _size_valid(_cached_path(Path(CACHE_DIR), item), item)
        print("CACHE HIT" if hit else "CACHE MISS", item["filename"], flush=True)
        if not hit:
            missing.append(item)
    return dict(items=items, missing=missing, published=published == revisions)


@app.function(image=image, serialized=False, volumes={"/data": volume.with_mount_options(sub_path="/comfyui")}, timeout=300, cpu=1.0, memory=1024)
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


def _hf_model_file(url):
    """Recognize public Hub model resolve URLs, including nested filenames."""
    import re
    from urllib.parse import parse_qsl, unquote, urlsplit

    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc.lower() != "huggingface.co":
        return None
    if any(key != "download" for key, _ in parse_qsl(parts.query, keep_blank_values=True)):
        return None
    match = re.fullmatch(r"/([\w.-]+(?:/[\w.-]+)?)/resolve/([^/]+)/(.+)", parts.path)
    if not match:
        return None
    repo, revision, filename = map(unquote, match.groups())
    if repo.split("/")[0] in {"datasets", "spaces"}:
        return None
    if any(segment in {"", ".", ".."} for segment in filename.split("/")) or "\\" in filename:
        return None
    return dict(repo_id=repo, revision=revision, filename=filename)


def _download_external(item, target):
    import httpx
    import re
    import time

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    marker = part.with_suffix(".part.resume")
    try:
        identity = json.loads(marker.read_text())
    except (FileNotFoundError, ValueError):
        identity = {}
    offset = part.stat().st_size if part.exists() else 0
    headers = {"Accept-Encoding": "identity"}
    if offset and isinstance(identity, dict) and identity.get("etag"):
        headers.update({"Range": f"bytes={offset}-", "If-Range": identity["etag"]})

    for attempt in range(2):
        with httpx.stream("GET", item["url"], headers=headers, follow_redirects=True,
                          timeout=120) as response:
            mode = None
            if response.status_code == 416 and "Range" in headers:
                if response.headers.get("Content-Range") == f"bytes */{offset}" and (
                        item.get("digest") or (str(response.url) == identity.get("url") and
                        response.headers.get("ETag") == identity["etag"])):
                    break
            elif response.status_code == 206:
                total = identity.get("size")
                if ("Range" in headers and total and
                        response.headers.get("Content-Range") == f"bytes {offset}-{total-1}/{total}" and
                        str(response.url) == identity["url"] and
                        response.headers.get("ETag", identity["etag"]) == identity["etag"]):
                    mode = "ab"
            elif response.status_code == 200:
                mode = "wb"
                length = response.headers.get("Content-Length")
                total = int(length) if length else item.get("size")
            else:
                response.raise_for_status()
                raise ValueError("Unexpected download response")

            if mode is None:
                if attempt:
                    raise ValueError("Invalid download range")
                headers = {"Accept-Encoding": "identity"}
                continue
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ValueError("Unexpected encoded model response")
            with part.open(mode) as stream:
                if mode == "wb":
                    marker.unlink(missing_ok=True)
                    etag = response.headers.get("ETag", "")
                    if total and re.fullmatch(r'"[^"]*"', etag):
                        marker.write_text(json.dumps(dict(url=str(response.url), etag=etag, size=total)))
                checkpoint = time.monotonic()
                for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                    stream.write(chunk)
                    if time.monotonic() - checkpoint >= 60:
                        stream.flush()
                        volume.commit()
                        checkpoint = time.monotonic()
            if total is not None and part.stat().st_size != total:
                raise ValueError("Incomplete external download")
            break
    try:
        _verify_content(part, item)
    except ValueError:
        marker.unlink(missing_ok=True)
        raise
    os.replace(part, target)
    marker.unlink(missing_ok=True)


@app.function(image=image, serialized=False, volumes={"/data": volume.with_mount_options(sub_path="/comfyui")}, timeout=86400, secrets=[hf_secret], cpu=4.0, memory=4096)
def download_shard(payload: dict) -> dict:
    """Stage external URLs only; declared HF models must use modal-benchmark."""
    import concurrent.futures
    import time

    items = payload["items"]
    if any(item["kind"] != "external" for item in items):
        raise ValueError("download_shard accepts external URLs only; use modal-benchmark for HF models")
    volume.reload()
    started = time.time()
    run_root = Path(SHARD_ROOT) / payload["run_id"]
    workers = max(1, min(8, int(payload.get("workers") or 2)))
    errors = []

    def fetch(item):
        stage = _stage_dir(run_root, item)
        target = _cached_path(stage, item)
        if _size_valid(target, item):
            try:
                _verify_content(target, item)
                return
            except ValueError:
                pass
        # Preserve explicit models_ext Hub URLs (including their local filenames).
        hub_file = _hf_model_file(item["url"])
        if hub_file:
            from huggingface_hub import hf_hub_download

            cached = hf_hub_download(**hub_file, local_dir=stage / ".hub",
                                     token=os.getenv("HF_TOKEN") or None)
            _verify_content(Path(cached), item)
            os.replace(cached, target)
        else:
            _download_external(item, target)

    def guarded(item):
        try:
            fetch(item)
        except Exception as exc:
            errors.append(f"{item['kind']}:{item['filename']}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items) or 1)) as pool:
        list(pool.map(guarded, items))
    volume.commit()  # Completed files and external partial files remain resumable.
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


def _require_benchmark_evidence(base: Path, items: list[dict]) -> None:
    """按 benchmark 0.4.2 的已提交清单与凭证校验，不重复读取大权重。

    Xet/HTTP 可信下载凭证不比较 mtime（Volume 跨容器时间戳可能漂移）；
    普通摘要凭证仍必须核对 mtime。这里不是逐字节的二次 SHA-256 审计。
    """
    import hashlib
    import re

    if not items:
        return
    manifest = benchmark_manifest(items)
    first = manifest["models"][0]
    saved_manifest = base / "download-manifest.json"
    if (not saved_manifest.is_file() or saved_manifest.is_symlink()
            or json.loads(saved_manifest.read_text(encoding="utf-8")) != manifest):
        raise RuntimeError("Benchmark persisted manifest mismatch")
    marker = (base / first["repo_id"] / first["revision"] /
              "all-components-complete.json")
    expected = {"status": "COMPLETE",
                "manifest_sha256": hashlib.sha256(
                    json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
                "verification_mode": "xet"}
    if not marker.is_file() or marker.is_symlink():
        raise RuntimeError(f"Missing benchmark completion marker: {marker}")
    if json.loads(marker.read_text(encoding="utf-8")) != expected:
        raise RuntimeError("Benchmark completion marker does not match this xet manifest")

    for item in items:
        repo_root = base / item["repo_id"] / item["revision"]
        source = benchmark_path(base, item)
        if not _size_valid(source, item):
            raise RuntimeError(f"Missing or wrong-size benchmark file: {source}")
        filename = item["filename"]
        receipt = (repo_root / "_verification" /
                   (hashlib.sha256(filename.encode("utf-8")).hexdigest() + ".json"))
        if not receipt.is_file() or receipt.is_symlink():
            raise RuntimeError(f"Missing benchmark verification receipt: {receipt}")
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        expected_sha = item["digest"] if item["algorithm"] == "sha256" else None
        expected_fields = {
            "version": 1, "repo_id": item["repo_id"],
            "revision": item["revision"], "filename": filename,
            "size": item["size"], "source_sha256": expected_sha,
        }
        if not isinstance(saved, dict) or any(saved.get(k) != v for k, v in expected_fields.items()):
            raise RuntimeError(f"Benchmark receipt metadata mismatch: {filename}")
        method = saved.get("method")
        if not isinstance(saved.get("mtime_ns"), int):
            raise RuntimeError(f"Invalid benchmark receipt mtime: {filename}")
        if method == "trusted_xet_download":
            if (set(saved) != set(expected_fields) | {"mtime_ns", "method", "xet_hash", "huggingface_hub", "hf_xet"}
                    or not isinstance(saved.get("xet_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", saved["xet_hash"])
                    or saved.get("huggingface_hub") != "1.33.0"
                    or saved.get("hf_xet") != "1.8.0"):
                raise RuntimeError(f"Invalid Xet transport receipt: {filename}")
        elif method == "trusted_hf_http_download":
            etag = saved.get("etag")
            if (set(saved) != set(expected_fields) | {"mtime_ns", "method", "etag", "huggingface_hub"}
                    or not isinstance(etag, str)
                    or not re.fullmatch(r"(?:[A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})", etag)
                    or saved.get("huggingface_hub") != "1.33.0"
                    or (expected_sha is not None and len(etag) == 64
                        and etag.lower() != expected_sha)):
                raise RuntimeError(f"Invalid HF HTTP receipt: {filename}")
        elif ((expected_sha is None or method != "sha256_match")
              or set(saved) != set(expected_fields) | {"mtime_ns", "method"}
              or saved["mtime_ns"] != source.stat().st_mtime_ns):
            raise RuntimeError(f"Invalid benchmark verification method: {filename}")


@app.function(image=image, serialized=False, volumes={"/data": volume.with_mount_options(sub_path="/comfyui")}, timeout=86400, secrets=[hf_secret], cpu=2.0, memory=8192)
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
    # Publish verified external files and recheck HF staging before moving it.
    pending = payload.get("downloaded_items", [])
    hf_pending = [item for item in pending if item["kind"] == "hf"]
    if hf_pending:
        if not payload.get("benchmark_base"):
            raise RuntimeError("HF publication requires benchmark evidence")
        phase = time.monotonic()
        _require_benchmark_evidence(Path(payload["benchmark_base"]), hf_pending)
        log("evidence", phase)
    phase = time.monotonic()
    print(f"MERGE publish: {len(pending)} files (same-volume rename)", flush=True)
    for item in pending:
        source = (benchmark_path(Path(payload["benchmark_base"]), item)
                  if item["kind"] == "hf" else
                  _cached_path(_stage_dir(run_root, item), item))
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
    cleanup_dirs = [run_root]
    if hf_pending:
        benchmark_stage = Path(payload["benchmark_base"])
        # 任务目录里可能残留已经复用的 blob；仅删除当前清单专属暂存。
        if (benchmark_stage.parent != root / ".benchmark-downloads"
                or benchmark_stage.is_symlink()):
            raise ValueError("Unsafe benchmark cleanup path")
        cleanup_dirs.append(benchmark_stage)
    for staged in cleanup_dirs:
        if staged.is_dir():
            phase = time.monotonic()
            print(f"MERGE cleanup: {staged}", flush=True)
            try:
                shutil.rmtree(staged)
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
    plan = fetch_manifest.remote(config_json, WORKFLOW, update)
    items = plan["items"]
    missing_items = plan["missing"]
    if not missing_items and plan["published"]:
        print(f"CACHE READY: {WORKFLOW} ({len(items)} models)", flush=True)
        return
    # Same manifest (files + pinned revisions) -> same run id, so a re-run
    # resumes into the same shard dirs; a different manifest is isolated.
    fingerprint = json.dumps(items, sort_keys=True)
    run_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:12]
    print(f"Run id: {run_id}")
    total = sum(int(it.get("size") or 0) for it in missing_items)
    print(f"Cache: {len(items) - len(missing_items)} hit(s), {len(missing_items)} missing")
    hf_items = [item for item in missing_items if item["kind"] == "hf"]
    external_items = [item for item in missing_items if item["kind"] == "external"]
    # 同一 Workflow 的缺失文件集合可能变化，下载目录必须绑定本次 HF 清单。
    hf_manifest = benchmark_manifest(hf_items)
    hf_id = hashlib.sha256(json.dumps(hf_manifest, sort_keys=True).encode()).hexdigest()[:16]
    # Benchmark 0.4.2 uses persistent Writers by default on Volume V2; all mounts use /comfyui.
    benchmark_base = f"{CACHE_DIR}/.benchmark-downloads/{hf_id}"
    if hf_items:
        hf_workers = hf_download_workers()
        print(f"Downloading {len(hf_items)} HF files with modal-benchmark (max {hf_workers} workers)", flush=True)
        download_hf_items(hf_items, benchmark_base, create_hf_downloader(hf_workers),
                          workers=hf_workers)
    shards = []
    if external_items:
        workers = _env_int("MODEL_DOWNLOAD_WORKERS", 2, 1, 8)
        target_gb = _env_int("MODEL_DOWNLOAD_SHARD_TARGET_GB", 16, 1, 1024)
        forced = _env_int("MODEL_DOWNLOAD_SHARDS", 0, 0, MAX_SHARDS)
        external_bytes = sum(int(item.get("size") or 0) for item in external_items)
        target = -(-external_bytes // forced) if forced else target_gb * 1_000_000_000
        shards = pack_shards(external_items, target, forced or MAX_SHARDS)
    print(f"Models: {len(items)} requested, {len(missing_items)} to download, {total / 1e9:.1f} GB, {len(shards)} shard(s)")
    payloads = []
    for i, shard in enumerate(shards):
        shard_bytes = sum(int(it.get("size") or 0) for it in shard)
        print(f"  shard {i}: {len(shard)} files, {shard_bytes / 1e9:.1f} GB")
        payloads.append({"shard_id": i, "items": shard, "run_id": run_id, "workers": workers})

    results = list(download_shard.map(payloads)) if payloads else []
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
                                  "downloaded_items": missing_items,
                                  "benchmark_base": benchmark_base})
    if not merged["ok"]:
        for m in merged["missing"]:
            print("MISSING after merge:", m)
        raise SystemExit("Merge verification failed; see lines above.")

    wall = time.time() - t0
    print(f"Downloaded: {len(missing_items)} files, {total / 1e9:.1f} GB in {wall:.1f}s "
          f"({total / 1e6 / max(wall, 0.1):.0f} MB/s aggregate) with {len(shards)} external URL shard(s)")
    print(f"Model cache Volume: {MODEL_VOLUME_NAME} (Volumes v2)")
