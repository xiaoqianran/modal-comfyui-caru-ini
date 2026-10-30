"""Run resumable ComfyUI Task JSON pipelines."""
import argparse
import hashlib
import http.client
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def request(url, data=None, headers=None, timeout=300, retries=6):
    """HTTP with retries: Modal cold starts can answer slowly or with 5xx/303."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode("utf-8", "replace")
            if e.code in (400, 401, 403, 404):  # not retryable
                raise RuntimeError(f"HTTP {e.code} {url}: {body}")
            last = RuntimeError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as e:
            last = e
        time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"Request failed after {retries} tries: {url}: {last}")


def get_json(base, path):
    return json.loads(request(base + path))


def upload(base, file):
    """Upload under a content-addressed name to avoid cross-task collisions."""
    file = Path(file)
    data = file.read_bytes()
    filename = f"runner_{hashlib.sha256(data).hexdigest()[:24]}{file.suffix.lower()}"
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode(),
        data,
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\nfalse\r\n--{boundary}--\r\n'.encode(),
    ]
    out = json.loads(request(
        base + "/upload/image", b"".join(parts),
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    ))
    return (out["subfolder"] + "/" if out.get("subfolder") else "") + out["name"]

def build_prompt(graph, sets, base, cache):
    prompt = json.loads(json.dumps(graph))
    for key, value in sets.items():
        node, _, field = key.partition(".")
        if node not in prompt or not field:
            raise KeyError(f"Bad target '{key}' (expected <node_id>.<input>, node must exist in graph)")
        if isinstance(value, str) and value.startswith("@"):
            path = value[1:]
            if path not in cache:
                cache[path] = upload(base, path)
            value = cache[path]
        prompt[node].setdefault("inputs", {})[field] = value
    return prompt


def collect_files(outputs):
    """Every {filename, subfolder, type} dict in any output list (images, gifs, video, 3d...)."""
    files = []
    for node in outputs.values():
        for items in node.values():
            if isinstance(items, list):
                files += [i for i in items if isinstance(i, dict) and "filename" in i]
    return files


def download(base, item, dest):
    q = urllib.parse.urlencode({
        "filename": item["filename"], "subfolder": item.get("subfolder", ""),
        "type": item.get("type", "output"),
    })
    data = request(f"{base}/view?{q}", timeout=600)
    if not data:
        raise RuntimeError("Empty download response")
    temp = dest.with_name(dest.name + ".part")
    temp.write_bytes(data)
    temp.replace(dest)


@contextmanager
def _task_lock(out):
    """Prevent concurrent runners for the same task output directory."""
    out.mkdir(parents=True, exist_ok=True)
    lock = out / ".runner.lock"
    with lock.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Task already running: {out}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

# Task pipeline mode: native ComfyUI graphs, independently routed endpoints.
def _save_json(path, value):
    """Persist task state atomically to survive interrupted requests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _safe_name(value):
    value = str(value)
    if not value or value in (".", "..") or any(c not in
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in value):
        raise ValueError(f"Invalid task/step/job name: {value!r}")
    return value


def _input_value(value, item, completed):
    """Resolve $item.key and $step.image or $step.job.image; preserve ordinary text."""
    if not isinstance(value, str) or not value.startswith("$"):
        return value
    parts = value[1:].split(".")
    if parts[0] == "item":
        if item is None or len(parts) != 2 or parts[1] not in item:
            raise ValueError(f"Unknown item reference: {value}")
        return item[parts[1]]
    step = parts[0]
    if step not in completed:
        raise ValueError(f"Unknown or unfinished step reference: {value}")
    jobs = completed[step]
    if len(parts) == 2 and len(jobs) == 1:
        job = next(iter(jobs))
        kind = parts[1]
    elif len(parts) == 3:
        job, kind = parts[1:]
    else:
        raise ValueError(f"Ambiguous output reference: {value}; use $step.job.image")
    if job not in jobs:
        raise ValueError(f"Unknown output job: {value}")
    paths = jobs[job]
    matches = [path for path in paths if
               (kind == "image" and path.suffix.lower() in IMAGE_EXT)
               or (kind == "video" and path.suffix.lower() in {".mp4", ".webm", ".mov"})
               or (kind == "file")]
    if len(matches) != 1:
        raise ValueError(f"Reference {value}: expected one {kind}, found {len(matches)}")
    return "@" + str(matches[0].resolve())


def _submit_job(key, job, completed, task_path, root, upload_caches):
    sid, jid = key
    resolved = {k: _input_value(v, job["entry"], completed)
                for k, v in job["settings"].items()}
    for field, value in resolved.items():
        if isinstance(value, str) and value.startswith("@"):
            file = Path(value[1:])
            if not file.is_absolute():
                local = task_path.parent / file
                file = local if local.is_file() else root / file
            if not file.is_file():
                raise FileNotFoundError(f"{sid}/{jid} input {field}: {file}")
            resolved[field] = "@" + str(file.resolve())
    prompt = build_prompt(job["graph"], resolved, job["base"],
                          upload_caches.setdefault(job["base"], {}))
    job["folder"].mkdir(parents=True, exist_ok=True)
    _save_json(job["folder"] / "workflow.json", prompt)
    _save_json(job["status_file"], {"status": "submitting", "endpoint": job["endpoint"]})
    body = json.dumps({"prompt": prompt, "client_id": uuid.uuid4().hex}).encode()
    try:
        reply = request(job["base"] + "/prompt", body,
                        {"Content-Type": "application/json"}, retries=1)
        pid = json.loads(reply)["prompt_id"]
    except Exception as exc:
        _save_json(job["status_file"], {"status": "uncertain",
                                        "endpoint": job["endpoint"], "error": str(exc)})
        raise RuntimeError(f"{sid}/{jid}: submission uncertain; inspect remote queue") from exc
    job["prompt_id"] = pid
    job["state"] = "submitted"
    job["submitted_at"] = time.monotonic()
    _save_json(job["status_file"], {"status": "submitted", "endpoint": job["endpoint"],
                                    "prompt_id": pid})
    print(f"{sid}/{jid}: submitted {pid}", flush=True)

def _check_job(key, job, completed, timeout):
    sid, jid = key
    if time.monotonic() - job["submitted_at"] > timeout:
        raise TimeoutError(f"{sid}/{jid}: timed out waiting for {job['prompt_id']}")
    history = get_json(job["base"], f"/history/{job['prompt_id']}")
    if job["prompt_id"] not in history:
        return False
    record = history[job["prompt_id"]]
    status = record.get("status", {})
    if status.get("status_str") == "error":
        raise RuntimeError(f"{sid}/{jid} remote error: {json.dumps(status)[:700]}")
    if not record.get("outputs"):
        return False
    output_files = collect_files(record["outputs"])
    if not output_files:
        raise RuntimeError(f"{sid}/{jid}: no downloadable output")
    local = []
    used = set()
    for i, item in enumerate(output_files):
        filename = Path(item["filename"]).name
        if filename in used:
            filename = f"{i:03d}_{filename}"
        used.add(filename)
        target = job["folder"] / filename
        if not target.is_file() or target.stat().st_size == 0:
            download(job["base"], item, target)
        local.append(target.resolve())
    _save_json(job["status_file"], {"status": "completed",
                                    "prompt_id": job["prompt_id"],
                                    "endpoint": job["endpoint"],
                                    "files": [str(p) for p in local]})
    completed[sid][jid] = local
    job["state"] = "completed"
    print(f"{sid}/{jid}: completed {len(local)} output(s)", flush=True)
    return True

def _run_task_unlocked(task_path, endpoints_path="endpoints.json", output=None, timeout=3600):
    """Run ComfyUI jobs as dependencies become ready, across independent endpoints."""
    task_path = Path(task_path).resolve()
    task = json.loads(task_path.read_text(encoding="utf-8-sig"))
    endpoints_file = Path(endpoints_path).resolve()
    endpoints = json.loads(endpoints_file.read_text(encoding="utf-8-sig"))
    root = endpoints_file.parent
    out = Path(output).resolve() if output else root / "outputs" / _safe_name(task["name"])
    steps = task["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("Task must contain nonempty steps")
    mode = task.get("mode", "stream")
    if mode not in ("batch", "stream"):
        raise ValueError(f"Invalid mode: {mode!r}; expected 'batch' or 'stream'")

    jobs = {}
    step_ids = set()
    for step in steps:
        sid = _safe_name(step["id"])
        if sid in step_ids:
            raise ValueError(f"Duplicate step id: {sid}")
        endpoint_name = step["endpoint"]
        if endpoint_name not in endpoints:
            raise KeyError(f"Unknown endpoint: {endpoint_name}")
        base = endpoints[endpoint_name].rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"Invalid endpoint: {endpoint_name}")
        graph = json.loads((root / step["workflow"]).read_text(encoding="utf-8-sig"))
        if "nodes" in graph:
            raise ValueError("Expected ComfyUI API JSON")
        entries = step.get("foreach", [{"name": "main"}])
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Empty foreach: {sid}")
        for entry in entries:
            jid = _safe_name(entry["name"])
            key = (sid, jid)
            if key in jobs:
                raise ValueError(f"Duplicate job: {sid}/{jid}")
            settings = dict(step.get("set", {}))
            settings.update(entry.get("set", {}))
            deps = set()
            for value in settings.values():
                if not isinstance(value, str) or not value.startswith("$") or value.startswith("$item."):
                    continue
                parts = value[1:].split(".")
                if len(parts) == 3:
                    dep = (parts[0], parts[1])
                elif len(parts) == 2:
                    upstream = [k for k in jobs if k[0] == parts[0]]
                    if len(upstream) != 1:
                        raise ValueError(f"Ambiguous reference: {value}")
                    dep = upstream[0]
                else:
                    raise ValueError(f"Bad reference: {value}")
                if dep not in jobs:
                    raise ValueError(f"Dependency must refer to an earlier job: {value}")
                deps.add(dep)
            folder = out / sid / jid
            jobs[key] = dict(entry=entry, settings=settings, deps=deps, base=base,
                             endpoint=endpoint_name, graph=graph, folder=folder,
                             status_file=folder / "status.json", state="pending")
        step_ids.add(sid)

    completed = {sid: {} for sid in step_ids}
    upload_caches = {}
    for key, job in jobs.items():
        status_file = job["status_file"]
        status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
        files = [Path(p) for p in status.get("files", [])]
        if status.get("status") == "completed" and files and all(p.is_file() and p.stat().st_size for p in files):
            completed[key[0]][key[1]] = files
            job["state"] = "completed"
            print(f"{key[0]}/{key[1]}: resumed complete", flush=True)
        elif status.get("prompt_id"):
            job["state"] = "submitted"
            job["prompt_id"] = status["prompt_id"]
            job["submitted_at"] = time.monotonic()
            print(f"{key[0]}/{key[1]}: recovering {status['prompt_id']}", flush=True)
        elif status.get("status") in ("submitting", "uncertain"):
            raise RuntimeError(f"{key[0]}/{key[1]}: uncertain prior submission, inspect remote queue")
    while any(job["state"] != "completed" for job in jobs.values()):
        progress = False
        for key, job in jobs.items():
            if job["state"] != "pending" or not all(dep[1] in completed[dep[0]] for dep in job["deps"]):
                continue
            if mode == "batch" and any(
                other["state"] != "completed"
                for dep_step in {dep[0] for dep in job["deps"]}
                for other_key, other in jobs.items() if other_key[0] == dep_step
            ):
                continue
            _submit_job(key, job, completed, task_path, root, upload_caches)
            progress = True

        # Poll every submitted job once. Never block later jobs behind the first job.
        for key, job in jobs.items():
            if job["state"] != "submitted":
                continue
            if _check_job(key, job, completed, timeout):
                progress = True
        if not progress:
            time.sleep(3)
    return out


def run_task(task_path, endpoints_path="endpoints.json", output=None, timeout=3600):
    """Single runner per output directory; recover safely after process exit."""
    if output:
        out = Path(output).resolve()
    else:
        name = _safe_name(json.loads(Path(task_path).read_text(encoding="utf-8-sig"))["name"])
        out = Path(endpoints_path).resolve().parent / "outputs" / name
    with _task_lock(out):
        return _run_task_unlocked(task_path, endpoints_path, output, timeout)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="Task JSON")
    ap.add_argument("--endpoints", default="endpoints.json")
    ap.add_argument("--out", help="Output directory (default: outputs/<task name>)")
    ap.add_argument("--timeout", type=int, default=3600, help="Seconds per job")
    args = ap.parse_args()
    run_task(args.task, args.endpoints, args.out, args.timeout)


if __name__ == "__main__":
    main()
