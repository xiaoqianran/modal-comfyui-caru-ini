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
volume = modal.Volume.from_name("modal-comfyui-models", create_if_missing=True)
app = modal.App("modal-model-cache-download")
try:
    hf_secret = modal.Secret.from_name("huggingface-secret")
    hf_secret.hydrate()
except modal.exception.NotFoundError:
    hf_secret = modal.Secret.from_dict({"HF_TOKEN": os.getenv("HF_TOKEN", "")})
image = modal.Image.debian_slim(python_version="3.11").pip_install("huggingface_hub>=0.30,<2", "httpx")

@app.function(image=image, volumes={"/cache": volume}, timeout=86400, secrets=[hf_secret])
def download_models(config_json: str):
    import concurrent.futures
    import hashlib
    import httpx
    from huggingface_hub import hf_hub_download

    data = json.loads(config_json)
    items = [("hf", m) for m in data.get("models", [])]
    items += [("external", m) for m in data.get("models_ext", [])]
    workers = int(os.getenv("MODEL_DOWNLOAD_WORKERS", "4"))
    if not 1 <= workers <= 6:
        raise ValueError("MODEL_DOWNLOAD_WORKERS must be 1..6")

    def fetch(item):
        kind, model = item
        if kind == "hf":
            hf_hub_download(repo_id=model["repo_id"], filename=model["filename"],
                            cache_dir="/cache", token=os.getenv("HF_TOKEN") or None)
            print("CACHED HF", model["repo_id"], model["filename"])
        else:
            target = Path("/cache") / model["filename"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size:
                print("CACHED external", target.name)
                return
            url = model["url"]
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
            print("CACHED external", target.name)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items) or 1)) as pool:
        list(pool.map(fetch, items))
    volume.commit()
    return len(items)


@app.local_entrypoint()
def main():
    if not CONFIG.is_file():
        raise ValueError(f"Missing workflow models.json: {CONFIG}")
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    print(f"Workflow: {WORKFLOW}")
    print(f"Models: {len(config.get('models', [])) + len(config.get('models_ext', []))}")
    print("Model cache Volume: modal-comfyui-models")
    print("Downloaded:", download_models.remote(json.dumps(config)))