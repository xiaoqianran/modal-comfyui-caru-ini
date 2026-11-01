"""Prewarm the H3 pipeline before Modal takes its GPU memory snapshot.

Modal's recommended server lifecycle is: start local server, wait for readiness,
issue bounded warmup requests, then return from @modal.enter(snap=True).
"""

import json
import shutil
import time
import urllib.request
import uuid
from pathlib import Path


def _json_request(base: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def warmup_h3(port: int, graph_path: str, output_root: str, timeout: int = 900):
    """Run one tiny genuine H3 inference; never snapshot while the queue is busy."""
    graph = json.loads(Path(graph_path).read_text(encoding="utf-8"))
    base = f"http://127.0.0.1:{port}"
    started = time.monotonic()
    prompt_id = None
    prefix_dir = Path(output_root) / "__snapshot_warmup__"
    print("SNAPSHOT_H3_WARMUP start: 5 frames, 1 sampling step", flush=True)
    try:
        result = _json_request(base, "/prompt", {
            "prompt": graph,
            "client_id": "snapshot_warmup_" + uuid.uuid4().hex,
        })
        if result.get("node_errors") or not result.get("prompt_id"):
            raise RuntimeError(f"Warmup prompt rejected: {result}")
        prompt_id = result["prompt_id"]
        deadline = started + timeout
        next_log = started + 30
        while time.monotonic() < deadline:
            history = _json_request(base, "/history/" + prompt_id)
            if prompt_id in history:
                record = history[prompt_id]
                status = record.get("status", {})
                if status.get("status_str") != "success" or not status.get("completed"):
                    raise RuntimeError(f"Warmup failed: {status}")
                if not record.get("outputs"):
                    raise RuntimeError("Warmup completed without output")
                # No queued/in-flight GPU work may remain when Modal freezes CUDA.
                queue = _json_request(base, "/queue")
                if queue.get("queue_running") or queue.get("queue_pending"):
                    time.sleep(1)
                    continue
                print(
                    f"SNAPSHOT_H3_WARMUP success in {time.monotonic() - started:.1f}s",
                    flush=True,
                )
                return
            if time.monotonic() >= next_log:
                queue = _json_request(base, "/queue")
                running = queue.get("queue_running", [])
                pending = queue.get("queue_pending", [])
                print(
                    f"SNAPSHOT_H3_WARMUP waiting {time.monotonic() - started:.0f}s "
                    f"(running={len(running)}, pending={len(pending)})",
                    flush=True,
                )
                next_log += 30
            time.sleep(3)
        raise TimeoutError(f"H3 warmup exceeded {timeout}s")
    except Exception:
        # During snap=True there are no external user prompts yet.
        if prompt_id:
            try:
                _json_request(base, "/interrupt", {})
            except (OSError, ValueError) as exc:
                print(f"SNAPSHOT_H3_WARMUP interrupt failed: {exc}", flush=True)
        print("SNAPSHOT_H3_WARMUP failed; refusing to snapshot a falsely warm model", flush=True)
        raise
    finally:
        # Do not persist the synthetic sample video in the output Volume.
        if prefix_dir.exists():
            shutil.rmtree(prefix_dir)
