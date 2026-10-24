"""Qwen-Image-2.1 ComfyUI remote benchmark: warm state only, JSON+CSV."""
import csv
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://xiaoshuhuaer--modal-comfyui-qwen-image-2-1-comfyui-ui.us-east.modal.run"
OUT = Path(__file__).resolve().parent / "qwen21_l40s_results"
OUT.mkdir(exist_ok=True)
CASES = [
    ("A_1024_20", 1024, 20),
    ("B_1024_25", 1024, 25),
    ("C_1024_35", 1024, 35),
    ("D_1536_25", 1536, 25),
    ("E_2048_25", 2048, 25),
]
PROMPT = "A photorealistic studio product photograph of a premium matte black camera on a polished concrete table, dramatic softbox lighting, fine surface textures, subtle shadows, sharp focus, editorial product photography"
SEED = 20261025


def request(path, payload=None, timeout=180):
    raw = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=raw, headers={"Content-Type": "application/json"} if raw else {})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def graph(size, steps, label):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "qwen_image_2.1_bf16.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "QwenImage21Cache", "inputs": {"model": ["1", 0], "device": "auto", "dtype": "default"}},
        "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_8b_bf16.safetensors", "type": "qwen_image", "device": "default"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_2.1_vae_bf16.safetensors"}},
        "5": {"class_type": "TextEncodeQwenImage21", "inputs": {"clip": ["3", 0], "prompt": PROMPT, "negative_prompt": "blurry, low quality, watermark", "resolution": 1024, "images": {}}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": size, "height": size, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {"model": ["2", 0], "positive": ["5", 0], "negative": ["5", 1], "latent_image": ["6", 0], "seed": SEED + (1 if label.startswith("WARMUP") else 0), "steps": steps, "cfg": 1, "sampler_name": "euler", "scheduler": "simple", "denoise": 1}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "qwen21_benchmark/" + label}},
    }


def run(label, size, steps):
    stats = request("/system_stats")
    device = stats["devices"][0]
    total = device["vram_total"]
    idle = (total - device["vram_free"]) / 2**30
    response = request("/prompt", {"prompt": graph(size, steps, label)})
    if "error" in response:
        raise RuntimeError(str(response))
    prompt_id = response["prompt_id"]
    start = time.perf_counter()
    peak = idle
    samples = 0
    while time.perf_counter() - start < 1500:
        try:
            s = request("/system_stats", timeout=10)["devices"][0]
            peak = max(peak, (s["vram_total"] - s["vram_free"]) / 2**30)
            samples += 1
        except Exception:
            pass
        try:
            history = request("/history/" + prompt_id, timeout=10)
            if prompt_id in history:
                item = history[prompt_id]
                status = item.get("status", {})
                images = sum(len(v.get("images", [])) for v in item.get("outputs", {}).values())
                row = {"case": label, "size": size, "steps": steps, "elapsed_sec": round(time.perf_counter() - start, 2), "idle_vram_gib": round(idle, 2), "sampled_peak_vram_gib": round(peak, 2), "samples": samples, "images": images, "status": status.get("status_str"), "messages": str(status.get("messages", []))[-700:]}
                print(json.dumps(row, ensure_ascii=False), flush=True)
                return row
        except Exception:
            pass
        time.sleep(0.5)
    raise TimeoutError(label)


def main():
    print("Benchmark started UTC:", datetime.utcnow().isoformat(), flush=True)
    print("Pre-warm: run 1024x1024 20 steps, excluded", flush=True)
    rows = []
    try:
        warm = run("WARMUP_NOT_COUNTED", 1024, 20)
        if warm["images"] < 1:
            raise RuntimeError("Warmup did not produce an image")
        for label, size, steps in CASES:
            try:
                rows.append(run(label, size, steps))
            except Exception as exc:
                rows.append({"case": label, "size": size, "steps": steps, "status": "FAILED", "messages": str(exc)})
                print("FAILED", label, str(exc), flush=True)
            (OUT / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
            with (OUT / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["case", "size", "steps", "elapsed_sec", "idle_vram_gib", "sampled_peak_vram_gib", "samples", "images", "status", "messages"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
    except Exception as exc:
        print("ABORTED", repr(exc), flush=True)
        raise


if __name__ == "__main__":
    main()
