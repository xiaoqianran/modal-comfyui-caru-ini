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
from contextvars import ContextVar, copy_context
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv"}
MODEL_EXT = {".glb", ".gltf", ".obj", ".stl", ".ply", ".splat", ".ksplat", ".spz"}

_DEADLINE = ContextVar("runner_deadline", default=None)


class RequestFailed(RuntimeError):
    """Transient transport failure; polling may retry on the next scan."""


def _remaining(limit):
    deadline = _DEADLINE.get()
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Batch deadline exceeded; saved prompt IDs remain resumable")
    return min(limit, remaining)


def request(url, data=None, headers=None, timeout=300, retries=6, destination=None):
    """HTTP with retries: Modal cold starts can answer slowly or with 5xx/303."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            with urllib.request.urlopen(req, timeout=_remaining(timeout)) as r:
                if destination is None:
                    result = r.read()
                else:
                    size = 0
                    digest = hashlib.sha256()
                    with destination.open("wb") as handle:
                        while True:
                            _remaining(timeout)
                            chunk = r.read(1024 * 1024)
                            if not chunk:
                                break
                            handle.write(chunk)
                            size += len(chunk)
                            digest.update(chunk)
                    expected = r.headers.get("Content-Length")
                    if not size or (expected is not None and size != int(expected)):
                        raise http.client.IncompleteRead(b"", int(expected or 1))
                    result = {"bytes": size, "sha256": digest.hexdigest()}
                _remaining(timeout)
                return result
        except urllib.error.HTTPError as e:
            body = e.read()[:500].decode("utf-8", "replace")
            if e.code in (400, 401, 403, 404):  # not retryable
                raise RuntimeError(f"HTTP {e.code} {url}: {body}")
            last = RuntimeError(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead) as e:
            last = e
        _remaining(timeout)
        if attempt + 1 < retries:
            time.sleep(_remaining(min(5 * (attempt + 1), 30)))
    raise RequestFailed(f"Request failed after {retries} tries: {url}: {last}")


def get_json(base, path):
    return json.loads(request(base + path, timeout=10, retries=1))


def upload(base, file):
    """Upload images through ComfyUI's standard image API."""
    file = Path(file)
    if file.suffix.lower() not in IMAGE_EXT:
        raise ValueError(
            f"Cannot upload {file.suffix} via standard ComfyUI /upload/image. "
            "Non-image inputs require a workflow-specific upload API.")
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
        if node not in prompt or not field or field not in prompt[node].get("inputs", {}):
            raise KeyError(f"Bad target '{key}': node and existing input field are required")
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
    temp = dest.with_name(dest.name + ".part")
    temp.unlink(missing_ok=True)
    try:
        evidence = request(f"{base}/view?{q}", timeout=60, destination=temp)
        temp.replace(dest)
        return evidence
    finally:
        temp.unlink(missing_ok=True)


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
               or (kind == "video" and path.suffix.lower() in VIDEO_EXT)
               or (kind == "model" and path.suffix.lower() in MODEL_EXT)
               or (kind == "file")]
    if len(matches) != 1:
        raise ValueError(f"Reference {value}: expected one {kind}, found {len(matches)}")
    return "@" + str(matches[0].resolve())


def _validate_graph(graph, settings, output_nodes=None):
    """Validate local API graph structure and task overrides before submission."""
    if not isinstance(graph, dict) or not graph:
        raise ValueError("Workflow must be a nonempty ComfyUI API graph")
    for node_id, node in graph.items():
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ValueError(f"Invalid graph node {node_id}: missing inputs")
        for field, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                if value[0] not in graph:
                    raise ValueError(f"Broken node link {node_id}.{field} -> {value[0]}")
    for key in settings:
        node_id, sep, field = key.partition(".")
        if not sep or node_id not in graph or field not in graph[node_id]["inputs"]:
            raise ValueError(f"Invalid task target {key}: node/input not found in workflow")
    if output_nodes is not None and (not isinstance(output_nodes, list) or not output_nodes):
        raise ValueError("output_nodes must be a nonempty list when specified")
    for node_id in output_nodes or ():
        if str(node_id) not in graph:
            raise ValueError(f"Invalid output node {node_id}: node not found in workflow")


def _job_intent_hash(job, jobs, task_dir, root):
    """Hash the requested graph, inputs and upstream jobs, not the saved output."""
    local_inputs = {}
    for field, raw in job["settings"].items():
        value = _input_value(raw, job["entry"], {}) if (
            isinstance(raw, str) and raw.startswith("$item.")) else raw
        if not isinstance(value, str) or not value.startswith("@"):
            continue
        path = Path(value[1:])
        if not path.is_absolute():
            candidate = task_dir / path
            path = candidate if candidate.is_file() else root / path
        if not path.is_file():
            raise FileNotFoundError(f"Input file for {field}: {path}")
        sha = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                sha.update(chunk)
        local_inputs[field] = sha.hexdigest()
    payload = {
        "version": 1, "endpoint": job["base"], "graph": job["graph"],
        "settings": job["settings"], "item": job["entry"],
        "local_inputs": local_inputs,
        "upstream": [(list(dep), jobs[dep]["intent_hash"]) for dep in sorted(job["deps"])],
    }
    if job.get("output_nodes") is not None:
        payload["output_nodes"] = job["output_nodes"]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    client_id = uuid.uuid4().hex
    identity = {"endpoint": job["endpoint"], "intent_hash": job["intent_hash"],
                "client_id": client_id}
    _save_json(job["status_file"], {"status": "submitting", **identity})
    body = json.dumps({"prompt": prompt, "client_id": client_id}).encode()
    started = time.monotonic()
    try:
        reply = request(job["base"] + "/prompt", body,
                        {"Content-Type": "application/json"}, retries=1)
        pid = json.loads(reply)["prompt_id"]
    except Exception as exc:
        _save_json(job["status_file"], {"status": "uncertain", **identity,
                                        "error": str(exc)})
        raise RuntimeError(f"{sid}/{jid}: submission uncertain; inspect remote queue") from exc
    job["prompt_id"] = pid
    job["state"] = "submitted"
    _save_json(job["status_file"], {"status": "submitted", **identity,
                                    "prompt_id": pid,
                                    "submit_seconds": time.monotonic() - started})
    print(f"{sid}/{jid}: submitted {pid}", flush=True)

def _read_outputs(key, job):
    """Poll a prompt without performing any output transfer."""
    sid, jid = key
    try:
        history = get_json(job["base"], f"/history/{job['prompt_id']}")
    except RequestFailed as exc:
        print(f"{sid}/{jid}: polling deferred: {exc}", flush=True)
        return None
    if job["prompt_id"] not in history:
        return None
    record = history[job["prompt_id"]]
    status = record.get("status", {})
    if status.get("status_str") == "error":
        raise RuntimeError(f"{sid}/{jid} remote error: {json.dumps(status)[:700]}")
    if not record.get("outputs"):
        if status.get("completed"):
            raise RuntimeError(f"{sid}/{jid}: no downloadable output")
        return None
    if status.get("completed") is False:
        return None
    outputs = record["outputs"]
    if job.get("output_nodes") is not None:
        outputs = {node: outputs[node] for node in job["output_nodes"] if node in outputs}
    files = collect_files(outputs)
    if not files:
        raise RuntimeError(f"{sid}/{jid}: no downloadable output")
    return [{"filename": f["filename"], "subfolder": f.get("subfolder", ""),
             "type": f.get("type", "output")} for f in files]


def _file_evidence(path):
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            _remaining(1)
            sha.update(chunk)
            size += len(chunk)
    return {"bytes": size, "sha256": sha.hexdigest()}


def _valid_artifact(target, evidence):
    return (isinstance(evidence, dict) and not target.is_symlink()
            and target.is_file() and isinstance(evidence.get("bytes"), int)
            and evidence["bytes"] > 0 and target.stat().st_size == evidence["bytes"]
            and _file_evidence(target) == {k: evidence.get(k) for k in ("bytes", "sha256")})


def _artifact_targets(job, files):
    used = {"status.json", "status.json.tmp", "workflow.json", "workflow.json.tmp"}
    for i, item in enumerate(files):
        filename = Path(item["filename"].replace("\\", "/")).name
        if filename in ("", ".", "..") or ":" in filename or filename.lower().endswith(".part"):
            raise ValueError(f"Unsafe output filename: {filename!r}")
        target_name = filename
        counter = i
        while target_name.casefold() in used:
            target_name = f"{counter:03d}_{filename}"
            counter += 1
        used.add(target_name.casefold())
        yield job["folder"] / target_name, item


def _collect_job(key, job, files):
    """Download independently of submit/poll; persist verified files incrementally."""
    sid, jid = key
    started = time.monotonic()
    path = job["status_file"]
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    previous = saved.get("artifacts", {}) if (
        saved.get("remote_files") == files
        and saved.get("prompt_id") == job["prompt_id"]
        and saved.get("intent_hash") == job["intent_hash"]
    ) else {}
    state = {"status": "collecting", "endpoint": job["endpoint"],
             "intent_hash": job["intent_hash"], "prompt_id": job["prompt_id"],
             "remote_files": files, "artifacts": dict(previous)}
    for field in ("client_id", "submit_seconds"):
        if field in saved:
            state[field] = saved[field]
    _save_json(path, state)
    local = []
    for target, item in _artifact_targets(job, files):
        evidence = previous.get(target.name)
        if not _valid_artifact(target, evidence):
            evidence = download(job["base"], item, target)
            # download returns the digest calculated during streaming, without a reread.
            if not isinstance(evidence, dict):
                evidence = _file_evidence(target)
            if not evidence["bytes"]:
                raise RuntimeError(f"Empty artifact: {target}")
        state["artifacts"][target.name] = evidence
        _save_json(path, state)
        local.append(target.resolve())
    state.update(status="completed", files=[str(p) for p in local],
                 collect_seconds=time.monotonic() - started)
    _save_json(path, state)
    print(f"{sid}/{jid}: completed {len(local)} output(s) in {state['collect_seconds']:.2f}s", flush=True)
    return local


def _check_job(key, job, completed):
    """Synchronous compatibility helper; the runner uses separate I/O pools."""
    files = _read_outputs(key, job)
    if files is None:
        return False
    completed[key[0]][key[1]] = _collect_job(key, job, files)
    job["state"] = "completed"
    return True


def _build_jobs(steps, endpoints, root, out, task_dir):
    """Expand task steps and validate all graphs/dependencies before execution."""
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
        _validate_graph(graph, {}, step.get("output_nodes"))
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
            _validate_graph(graph, settings, step.get("output_nodes"))
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
                             output_nodes=sorted({str(n) for n in step["output_nodes"]}) if step.get("output_nodes") is not None else None,
                             endpoint=endpoint_name, graph=graph, folder=folder,
                             status_file=folder / "status.json", state="pending")
        step_ids.add(sid)

    for job in jobs.values():
        job["intent_hash"] = _job_intent_hash(job, jobs, task_dir, root)

    return jobs, step_ids


def _run_task_unlocked(task_path, endpoints_path="endpoints.json", output=None,
                       timeout=0, trust_legacy_status=False):
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
    if timeout < 0:
        raise ValueError("Batch timeout must be >= 0 (0 disables timeout)")

    jobs, step_ids = _build_jobs(steps, endpoints, root, out, task_path.parent)

    # Validate *every* persisted job before submitting anything or adopting legacy state.
    statuses = {}
    for key, job in jobs.items():
        path = job["status_file"]
        status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        statuses[key] = status
        if not status:
            continue
        saved = status.get("intent_hash")
        if saved and saved != job["intent_hash"]:
            raise RuntimeError(
                f"{key[0]}/{key[1]}: Task/Workflow/input changed since last run. "
                "Existing outputs are untouched; use a new task name or --out path.")
        if not saved and not trust_legacy_status:
            raise RuntimeError(
                f"{key[0]}/{key[1]}: legacy status without intent hash. "
                "Review the old task and rerun with --trust-legacy-status to adopt it.")

    completed = {sid: {} for sid in step_ids}
    upload_caches = {}
    for key, job in jobs.items():
        status = statuses[key]
        if status and "intent_hash" not in status:
            status["intent_hash"] = job["intent_hash"]
            _save_json(job["status_file"], status)
        files = [Path(p) for p in status.get("files", [])]
        if any(p.resolve().parent != job["folder"].resolve() for p in files):
            raise RuntimeError(f"{key}: saved output path is outside its job directory")
        artifacts = status.get("artifacts")
        valid = bool(files) and all(p.is_file() and not p.is_symlink() and p.stat().st_size for p in files)
        if valid and artifacts is not None:
            valid = all(_valid_artifact(p, artifacts.get(p.name)) for p in files)
        if status.get("status") == "completed" and valid:
            completed[key[0]][key[1]] = files
            job["state"] = "completed"
            quality = "SHA-256 verified" if artifacts is not None else "legacy size-only"
            print(f"{key[0]}/{key[1]}: resumed complete ({quality})", flush=True)
        elif status.get("prompt_id"):
            job["prompt_id"] = status["prompt_id"]
            job["remote_files"] = status.get("remote_files")
            job["state"] = "collecting" if job["remote_files"] else "submitted"
            print(f"{key[0]}/{key[1]}: recovering {status['prompt_id']}", flush=True)
        elif status.get("status") in ("submitting", "uncertain", "completed", "collecting"):
            raise RuntimeError(f"{key[0]}/{key[1]}: prior state cannot be safely recovered; inspect status and remote queue")
    jobs_by_step = {}
    for key, job in jobs.items():
        jobs_by_step.setdefault(key[0], []).append(job)
    # Three independent bounded I/O pools. Submit in order per endpoint so shared
    # uploads and native ComfyUI queue ordering remain safe. Different endpoints,
    # history polls and artifact transfers cannot block one another.
    active = {}
    busy_endpoints = set()
    next_poll = {}
    limits = {"submit": 4, "poll": 8, "collect": 4}
    with ThreadPoolExecutor(max_workers=limits["submit"]) as submits, \
         ThreadPoolExecutor(max_workers=limits["poll"]) as polls, \
         ThreadPoolExecutor(max_workers=limits["collect"]) as transfers:
        pools = {"submit": submits, "poll": polls, "collect": transfers}
        def schedule(kind, key, fn, *args):
            active[key] = (kind, pools[kind].submit(copy_context().run, fn, *args))
        while active or any(job["state"] != "completed" for job in jobs.values()):
            _remaining(1)
            for key, (kind, future) in list(active.items()):
                if not future.done():
                    continue
                del active[key]
                job = jobs[key]
                result = future.result()
                if kind == "submit":
                    busy_endpoints.remove(job["base"])
                elif kind == "poll":
                    if result is not None:
                        job["remote_files"] = result
                        job["state"] = "collecting"
                    else:
                        next_poll[key] = time.monotonic() + 1
                else:
                    completed[key[0]][key[1]] = result
                    job["state"] = "completed"
            counts = {kind: sum(k == kind for k, _ in active.values()) for kind in limits}
            for key, job in jobs.items():
                if key in active:
                    continue
                state = job["state"]
                if state == "pending" and counts["submit"] < limits["submit"]:
                    if job["base"] in busy_endpoints or not all(dep[1] in completed[dep[0]] for dep in job["deps"]):
                        continue
                    if mode == "batch" and any(
                        other["state"] != "completed"
                        for dep_step in {dep[0] for dep in job["deps"]}
                        if dep_step != key[0]
                        for other in jobs_by_step[dep_step]
                    ):
                        continue
                    busy_endpoints.add(job["base"])
                    schedule("submit", key, _submit_job, key, job, completed, task_path, root, upload_caches)
                    counts["submit"] += 1
                elif state == "submitted" and counts["poll"] < limits["poll"] and time.monotonic() >= next_poll.get(key, 0):
                    schedule("poll", key, _read_outputs, key, job)
                    counts["poll"] += 1
                elif state == "collecting" and counts["collect"] < limits["collect"]:
                    schedule("collect", key, _collect_job, key, job, job["remote_files"])
                    counts["collect"] += 1
            if active:
                wait([f for _, f in active.values()], timeout=_remaining(0.25), return_when=FIRST_COMPLETED)
            elif any(job["state"] != "completed" for job in jobs.values()):
                # Only delayed polls may leave a valid graph with no active work.
                if not any(job["state"] == "submitted" for job in jobs.values()):
                    raise RuntimeError("Task scheduler stalled without a submitted job")
                time.sleep(_remaining(0.25))
    return out


def run_task(task_path, endpoints_path="endpoints.json", output=None,
             timeout=0, trust_legacy_status=False):
    """Single runner per output directory; recover safely after process exit."""
    if output:
        out = Path(output).resolve()
    else:
        name = _safe_name(json.loads(Path(task_path).read_text(encoding="utf-8-sig"))["name"])
        out = Path(endpoints_path).resolve().parent / "outputs" / name
    with _task_lock(out):
        token = _DEADLINE.set(time.monotonic() + timeout if timeout > 0 else None)
        try:
            return _run_task_unlocked(task_path, endpoints_path, output, timeout,
                                      trust_legacy_status=trust_legacy_status)
        finally:
            _DEADLINE.reset(token)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", required=True, help="Task JSON")
    ap.add_argument("--endpoints", default="endpoints.json")
    ap.add_argument("--out", help="Output directory (default: outputs/<task name>)")
    ap.add_argument("--timeout", type=int, default=0,
                    help="Total batch deadline in seconds (default: 0, disabled; queue time included)")
    ap.add_argument("--trust-legacy-status", action="store_true",
                    help="Explicitly adopt old status files that have no task fingerprint")
    args = ap.parse_args()
    run_task(args.task, args.endpoints, args.out, args.timeout, args.trust_legacy_status)


if __name__ == "__main__":
    main()
