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



def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.getenv("COMFY_URL"), help="Deployed app URL (or env COMFY_URL)")
    ap.add_argument("--graph", required=True, help="ComfyUI API-format JSON")
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
