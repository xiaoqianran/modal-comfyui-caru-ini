"""Batch-submit ComfyUI API graphs to a deployed Modal app and download results locally.

Standard library only. Usage:

  python batch_run.py --url https://<ws>--modal-<wf>-comfyui-ui.<region>.modal.run \
      --graph my_api_graph.json --jobs jobs.json --out results

graph: a ComfyUI *API format* JSON (ComfyUI menu: Workflow > Export (API)).
jobs:  JSON list; each job is {"name": "optional", "set": {"<node_id>.<input>": value}}.
       A string value starting with "@" is a local file: it is uploaded and the
       server-side filename is substituted (use for LoadImage etc.).
       Other values are written as-is. Seed-like inputs are NOT randomized unless
       you pass --random-seed "<node_id>.<input>".
Alternatively --images DIR with --image-input "<node_id>.image" makes one job per image.
For text generation: --prompt TEXT, --prompts FILE.txt (one per line), or
--shots FILE.csv (shot_id,prompt columns). Uses the unique *.prompt input in the
API workflow, or --prompt-input NODE.prompt if there are multiple.
Re-running skips jobs whose output folder already has a DONE marker.

Python example:
  from batch_run import generate
  generate(url="https://your-comfyui-url", workflow="workflow_api.json",
           prompts=["A red apple", "A blue flower"], output="results/story")
"""
import argparse
import csv
import json
import mimetypes
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
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
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        time.sleep(min(5 * (attempt + 1), 30))
    raise RuntimeError(f"Request failed after {retries} tries: {url}: {last}")


def get_json(base, path):
    return json.loads(request(base + path))


def upload(base, file):
    """Upload a local file via /upload/image; returns the server-side name."""
    file = Path(file)
    if not file.is_file():
        raise FileNotFoundError(file)
    boundary = uuid.uuid4().hex
    mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{file.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n".encode(),
        file.read_bytes(),
        f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n--{boundary}--\r\n'.encode(),
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
    dest.write_bytes(request(f"{base}/view?{q}", timeout=600))


def wait_done(base, pid, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        hist = get_json(base, f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            status = entry.get("status", {})
            if status.get("status_str") == "error":
                msgs = [m for m in status.get("messages", []) if m and m[0] == "execution_error"]
                raise RuntimeError("ComfyUI execution error: " + json.dumps(msgs[-1][1] if msgs else status)[:600])
            if entry.get("outputs"):
                return entry["outputs"]
        time.sleep(3)
    raise TimeoutError(f"prompt {pid} not finished in {timeout}s")


def prompt_target(graph, target):
    """Find a unique prompt input unless a node.input target is supplied."""
    if target:
        node, sep, field = target.partition(".")
        if not sep or node not in graph or field not in graph[node].get("inputs", {}):
            raise ValueError(f"Invalid --prompt-input: {target}")
        return target
    matches = [f"{node}.prompt" for node, data in graph.items()
               if isinstance(data, dict) and isinstance(data.get("inputs", {}).get("prompt"), str)]
    if len(matches) != 1:
        raise ValueError(f"Found {len(matches)} prompt inputs; specify --prompt-input NODE.prompt")
    return matches[0]


def load_jobs(args, graph):
    if args.jobs:
        jobs = json.loads(Path(args.jobs).read_text(encoding="utf-8-sig"))
    elif args.images:
        if not args.image_input:
            sys.exit("--images requires --image-input '<node_id>.<input>'")
        files = sorted(p for p in Path(args.images).iterdir() if p.suffix.lower() in IMAGE_EXT)
        jobs = [{"name": p.stem, "set": {args.image_input: "@" + str(p)}} for p in files]
    else:
        target = prompt_target(graph, args.prompt_input)
        if args.prompt is not None:
            prompts = [("prompt_001", args.prompt)]
        elif args.prompts:
            lines = Path(args.prompts).read_text(encoding="utf-8-sig").splitlines()
            prompts = [(f"shot_{i:03d}", line.strip()) for i, line in enumerate(lines, 1) if line.strip()]
        elif args.shots:
            with Path(args.shots).open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames or not {"shot_id", "prompt"}.issubset(reader.fieldnames):
                    raise ValueError("CSV needs shot_id,prompt columns")
                prompts = [(row["shot_id"].strip(), row["prompt"].strip()) for row in reader
                           if row["prompt"] and row["prompt"].strip()]
        else:
            sys.exit("Provide --jobs, --images, --prompt, --prompts, or --shots")
        if any(not name for name, _ in prompts) or len({name for name, _ in prompts}) != len(prompts):
            raise ValueError("Shot names must be nonempty and unique")
        jobs = [{"name": name, "set": {target: prompt}} for name, prompt in prompts]
    if not isinstance(jobs, list) or not jobs:
        sys.exit("No jobs found")
    return jobs


def generate(url, workflow, prompts, output="results", *, seed=None, timeout=3600):
    """Generate one image per text prompt using an API-format ComfyUI workflow.

    prompts: list of strings. Outputs go to output/shot_001, output/shot_002, etc.
    A fixed seed is optional; by default each shot gets a random seed if a seed
    input is present, preventing identical runs when the template uses a fixed seed.
    """
    graph = json.loads(Path(workflow).read_text(encoding="utf-8-sig"))
    if "nodes" in graph:
        raise ValueError("Export the workflow in ComfyUI API format")
    target = prompt_target(graph, None)
    if isinstance(prompts, (str, bytes)) or not isinstance(prompts, (list, tuple)) or not prompts:
        raise ValueError("prompts must be a nonempty list of strings")
    if any(not isinstance(p, str) or not p.strip() for p in prompts):
        raise ValueError("Each prompt must be a nonempty string")
    jobs = [{"name": f"shot_{i:03d}", "set": {target: prompt}}
            for i, prompt in enumerate(prompts, 1)]
    # Apply the sampler seed without assuming node IDs across workflows.
    seeds = [f"{node}.seed" for node, data in graph.items()
             if isinstance(data, dict) and "seed" in data.get("inputs", {})]
    if seed is not None:
        if len(seeds) != 1:
            raise ValueError(f"Expected one seed input, found {len(seeds)}")
        for job in jobs:
            job["set"][seeds[0]] = seed
    random_seeds = seeds if seed is None and len(seeds) == 1 else []
    failed = run_jobs(url, graph, jobs, output, random_seeds, timeout)
    if failed:
        raise RuntimeError(f"{failed} of {len(jobs)} jobs failed")
    return Path(output)


def run_jobs(base, graph, jobs, output, random_seed=(), timeout=3600):
    """Submit queued jobs, then download completed outputs."""
    base = base.rstrip("/")
    out = Path(output)
    cache, failed = {}, 0

    print("Connecting (first request may wait for a cold start)...", flush=True)
    request(base + "/system_stats", timeout=300, retries=12)

    # Submit everything first so ComfyUI's own queue keeps the GPU busy
    # (and the container does not scale down between jobs).
    pending = []
    for i, job in enumerate(jobs, 1):
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(job.get("name") or f"job{i:03d}"))
        folder = out / name
        if (folder / "DONE").exists():
            print(f"[{i}/{len(jobs)}] {name}: already done, skip")
            continue
        try:
            sets = dict(job.get("set", {}))
            for target in random_seed:
                sets.setdefault(target, random.randint(0, 2**53))
            prompt = build_prompt(graph, sets, base, cache)
            body = json.dumps({"prompt": prompt, "client_id": uuid.uuid4().hex}).encode()
            pid = json.loads(request(base + "/prompt", body, {"Content-Type": "application/json"}))["prompt_id"]
            pending.append((i, name, folder, pid))
            print(f"[{i}/{len(jobs)}] {name}: queued {pid}", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(jobs)}] {name}: SUBMIT FAILED: {e}", file=sys.stderr)

    for i, name, folder, pid in pending:
        try:
            outputs = wait_done(base, pid, timeout)
            folder.mkdir(parents=True, exist_ok=True)
            files = collect_files(outputs)
            for item in files:
                download(base, item, folder / Path(item["filename"]).name)
            (folder / "prompt_id.txt").write_text(pid)
            (folder / "DONE").write_text("")
            print(f"[{i}/{len(jobs)}] {name}: {len(files)} file(s) -> {folder}", flush=True)
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(jobs)}] {name}: FAILED: {e}", file=sys.stderr)

    print(f"Finished. {len(pending)} submitted, {failed} failed.")
    return failed



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


def _input_value(value, item, completed, task_dir):
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


def run_task(task_path, endpoints_path="endpoints.json", output=None, timeout=3600):
    """Execute sequential steps across endpoints with persisted per-job resume state."""
    task_path = Path(task_path).resolve()
    task = json.loads(task_path.read_text(encoding="utf-8-sig"))
    endpoints_file = Path(endpoints_path).resolve()
    endpoints = json.loads(endpoints_file.read_text(encoding="utf-8-sig"))
    root = endpoints_file.parent
    name = _safe_name(task["name"])
    steps = task["steps"]
    if not isinstance(steps, list) or not steps:
        raise ValueError("Task must contain nonempty steps")
    out = Path(output).resolve() if output else root / "outputs" / name
    completed = {}
    seen_steps = set()
    upload_caches = {}
    for step in steps:
        sid = _safe_name(step["id"])
        if sid in seen_steps:
            raise ValueError(f"Duplicate step id: {sid}")
        seen_steps.add(sid)
        endpoint_name = step["endpoint"]
        if endpoint_name not in endpoints:
            raise KeyError(f"Unknown endpoint: {endpoint_name}")
        base = endpoints[endpoint_name].rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ValueError(f"Endpoint must be an HTTP URL: {endpoint_name}")
        wf_path = root / step["workflow"]
        graph = json.loads(wf_path.read_text(encoding="utf-8-sig"))
        if "nodes" in graph:
            raise ValueError(f"Workflow must be ComfyUI API JSON: {wf_path}")
        entries = step.get("foreach", [{"name": "main"}])
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Invalid foreach for step {sid}")
        step_results = {}
        for entry in entries:
            jid = _safe_name(entry["name"])
            if jid in step_results:
                raise ValueError(f"Duplicate job name: {sid}/{jid}")
            folder = out / sid / jid
            status_file = folder / "status.json"
            status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
            # Never trust a stale DONE marker without all output files.
            if status.get("status") == "completed" and all(Path(p).is_file() for p in status.get("files", [])) and status.get("files"):
                step_results[jid] = [Path(p) for p in status["files"]]
                print(f"{sid}/{jid}: resumed complete", flush=True)
                continue
            # Inputs are resolved only for new submissions. Existing remote IDs are
            # authoritative: a crash after submission must not re-submit work.
            if status.get("status") in ("submitting", "uncertain") and not status.get("prompt_id"):
                raise RuntimeError(f"{sid}/{jid}: submission status uncertain; inspect remote queue before retrying")
            if status.get("prompt_id"):
                pid = status["prompt_id"]
                print(f"{sid}/{jid}: recovering {pid}", flush=True)
            else:
                settings = dict(step.get("set", {}))
                settings.update(entry.get("set", {}))
                resolved = {k: _input_value(v, entry, completed, task_path.parent) for k, v in settings.items()}
                # Resolve explicit local-file inputs relative to the task file first,
                # then the project root for convenient reusable references.
                for key, value in resolved.items():
                    if isinstance(value, str) and value.startswith("@"):
                        path = Path(value[1:])
                        if not path.is_absolute():
                            local = task_path.parent / path
                            path = local if local.is_file() else root / path
                        if not path.is_file():
                            raise FileNotFoundError(f"{sid}/{jid} input {key}: {path}")
                        resolved[key] = "@" + str(path.resolve())
                prompt = build_prompt(graph, resolved, base, upload_caches.setdefault(base, {}))
                folder.mkdir(parents=True, exist_ok=True)
                _save_json(folder / "workflow.json", prompt)
                # A POST response and local disk write aren't transactional.
                # Record pre-submit intent; ambiguous transport failures are not retried automatically.
                _save_json(status_file, {"status": "submitting", "endpoint": endpoint_name})
                body = json.dumps({"prompt": prompt, "client_id": uuid.uuid4().hex}).encode()
                try:
                    # One attempt here avoids hidden duplicate submits on network failure.
                    reply = request(base + "/prompt", body, {"Content-Type": "application/json"}, retries=1)
                    pid = json.loads(reply)["prompt_id"]
                except Exception as e:
                    _save_json(status_file, {"status": "uncertain", "endpoint": endpoint_name, "error": str(e)})
                    raise RuntimeError(f"{sid}/{jid} submission uncertain; inspect remote queue before retrying") from e
                status = {"status": "submitted", "prompt_id": pid, "endpoint": endpoint_name}
                _save_json(status_file, status)
                print(f"{sid}/{jid}: submitted {pid}", flush=True)
            outputs = wait_done(base, pid, timeout)
            files = collect_files(outputs)
            if not files:
                raise RuntimeError(f"{sid}/{jid}: no downloadable output")
            local = []
            used_names = set()
            for i, file in enumerate(files):
                filename = Path(file["filename"]).name
                if filename in used_names:
                    filename = f"{i:03d}_{filename}"
                used_names.add(filename)
                target = folder / filename
                if not target.is_file() or target.stat().st_size == 0:
                    download(base, file, target)
                local.append(target.resolve())
            _save_json(status_file, {"status": "completed", "prompt_id": pid,
                                     "endpoint": endpoint_name, "files": [str(p) for p in local]})
            step_results[jid] = local
            print(f"{sid}/{jid}: completed {len(local)} output(s)", flush=True)
        completed[sid] = step_results
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("COMFY_URL"), help="Deployed app URL (or env COMFY_URL)")
    ap.add_argument("--task", help="Pipeline task JSON (uses --endpoints)")
    ap.add_argument("--endpoints", default="endpoints.json", help="Endpoint alias JSON")
    ap.add_argument("--graph", help="ComfyUI API-format JSON")
    inputs = ap.add_mutually_exclusive_group()
    inputs.add_argument("--jobs", help="jobs JSON list")
    inputs.add_argument("--images", help="folder of images, one job per image")
    inputs.add_argument("--prompt", help="one text prompt")
    inputs.add_argument("--prompts", help="TXT, one prompt per line")
    inputs.add_argument("--shots", help="CSV with shot_id,prompt columns")
    ap.add_argument("--prompt-input", help="node_id.prompt; auto-detects if unique")
    ap.add_argument("--image-input", help="target for --images, e.g. 10.image")
    ap.add_argument("--random-seed", action="append", default=[], help="<node_id>.<input> to randomize per job")
    ap.add_argument("--out", default="results")
    ap.add_argument("--timeout", type=int, default=3600, help="per-job seconds")
    args = ap.parse_args()
    if args.task:
        run_task(args.task, args.endpoints, None if args.out == "results" else args.out, args.timeout)
        return
    if not args.graph:
        sys.exit("--graph is required unless --task is provided")
    if not args.url:
        sys.exit("--url (or COMFY_URL) is required")
    base = args.url.rstrip("/")

    graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    if "nodes" in graph:
        sys.exit("This is a UI-format workflow. In ComfyUI use Workflow > Export (API) and pass that file.")
    jobs = load_jobs(args, graph)
    failed = run_jobs(base, graph, jobs, args.out, args.random_seed, args.timeout)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
