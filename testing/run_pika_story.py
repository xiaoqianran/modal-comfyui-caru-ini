"""Resume-safe ComfyUI Pika image-to-video runner. No external dependencies."""
import argparse
import json
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "outputs/qwen21_story_16_sunset"
DEFAULT_URL = "https://xiaotaiyangqaq--comfyui-1.us-east.modal.run"


def request(base, path, payload=None, *, binary=False, timeout=90):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers), timeout=timeout) as resp:
                return resp.read() if binary else json.load(resp)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def upload(base, path):
    boundary = "----Pika" + uuid.uuid4().hex
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\nContent-Type: image/png\r\n\r\n").encode()
    body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(base + "/upload/image?overwrite=true", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)["name"]


def save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def build(template, job, image):
    graph = json.loads(json.dumps(template))
    node = graph["140:131"]["inputs"]
    node.update({"first_frame": ["image_input", 0], "prompt": job["prompt"],
                 "width": 768, "height": 1376})
    node.pop("last_frame", None)
    graph["image_input"] = {"class_type": "LoadImage", "inputs": {"image": image}}
    graph["92"]["inputs"]["filename_prefix"] = f"video/story_pika_timeline_{job['scene']:02d}"
    return graph


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument("--poll", type=int, default=10)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    jobs = json.loads((ROOT / "pika_video_jobs.json").read_text(encoding="utf-8"))
    out = ROOT / "pika_videos_timeline"
    out.mkdir(exist_ok=True)
    manifest = out / "submissions.json"
    state = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    valid = []
    for job in jobs:
        key = f"{job['scene']:02d}"
        image = Path(job["first_frame"])
        if not image.is_file() or not job["prompt"].strip():
            print("SKIP invalid image or prompt", key, flush=True)
            continue
        valid.append((key, job, image))

    if not args.collect_only:
        history = request(base, "/history?max_items=20")
        template = next((e["prompt"][2] for e in history.values()
                         if "140:143" in e.get("prompt", [None, None, {}])[2]
                         and "140:131" in e["prompt"][2]), None)
        if template is None:
            raise RuntimeError("Pika API template not found in recent history")
        for key, job, image in valid:
            if key in state or (out / (key + ".mp4")).exists():
                continue
            filename = upload(base, image)
            result = request(base, "/prompt", {"prompt": build(template, job, filename),
                                               "client_id": str(uuid.uuid4())})
            if "prompt_id" not in result:
                raise RuntimeError(f"Submission failed for {key}: {result}")
            state[key] = result["prompt_id"]
            save(manifest, state)
            print("QUEUED", key, result["prompt_id"], flush=True)

    pending = {key: pid for key, pid in state.items() if not (out / (key + ".mp4")).exists()}
    print("TRACKING", len(pending), "tasks", flush=True)
    while pending:
        try:
            history = request(base, "/history?max_items=100")
        except (urllib.error.URLError, TimeoutError) as exc:
            print("RETRY history", str(exc), flush=True)
            time.sleep(args.poll)
            continue
        for key, pid in list(pending.items()):
            entry = history.get(pid)
            if entry is None:
                entry = request(base, "/history/" + pid).get(pid)
            if entry is None:
                continue
            status = entry.get("status", {}).get("status_str")
            if status != "success":
                print("FAILED", key, status, flush=True)
                (out / (key + ".error.json")).write_text(json.dumps(entry.get("status", {}), indent=2), encoding="utf-8")
                del pending[key]
                continue
            videos = [f for output in entry.get("outputs", {}).values()
                      for f in output.get("images", []) if f.get("filename", "").endswith(".mp4")]
            if not videos:
                print("MISSING_VIDEO", key, flush=True)
                del pending[key]
                continue
            params = urllib.parse.urlencode({k: videos[0].get(k, "") for k in ("filename", "subfolder", "type")})
            target = out / (key + ".mp4")
            try:
                data = request(base, "/view?" + params, binary=True, timeout=180)
                if not data:
                    raise ValueError("Empty video")
                tmp = target.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(target)
                print("DOWNLOADED", key, len(data), flush=True)
                del pending[key]
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                print("RETRY download", key, str(exc), flush=True)
        if pending:
            time.sleep(args.poll)
    print("COMPLETE", flush=True)


if __name__ == "__main__":
    main()

