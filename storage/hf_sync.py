"""Replicate completed ComfyUI 3D outputs from the primary R2 mount to HF S3.

The source directory is R2-backed; HF is best-effort and never blocks inference.
"""
import logging
import os
import signal
import time
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig

SOURCE = Path(os.getenv("COMFY_3D_OUTPUT", "/root/comfy/ComfyUI/output/3d"))
PREFIX = f"comfyui/{os.getenv('WORKFLOW', 'comfyui-pixal3d-trellis2')}/3d/"
BUCKET = os.getenv("HF_S3_BUCKET", "comfyui-assets")
ENDPOINT = os.getenv("HF_S3_ENDPOINT", "https://s3.hf.co/seachen")
EXTENSIONS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz"}
POLL = int(os.getenv("HF_SYNC_INTERVAL", "15"))
LOG = logging.getLogger("hf-s3-sync")
RUNNING = True


def stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def client():
    return boto3.client(
        "s3", endpoint_url=ENDPOINT,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=boto3.session.Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def sync_once(s3, observed, completed, now=None):
    """Two equal consecutive metadata observations ensure the writer has settled.

    completed is deliberately process-local: subsequent container runs can retry
    files left on the persistent R2 mount.
    """
    now = time.monotonic() if now is None else now
    found = set()
    for path in SOURCE.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        relative = path.relative_to(SOURCE).as_posix()
        found.add(relative)
        stat = path.stat()
        if stat.st_size == 0:
            continue
        version = (stat.st_size, stat.st_mtime_ns)
        if completed.get(relative) == version:
            continue
        previous = observed.get(relative)
        observed[relative] = (version, now)
        if previous is None or previous[0] != version or now - previous[1] < POLL:
            continue
        key = PREFIX + relative
        try:
            # Streaming transfer; do not load large point clouds into RAM.
            s3.upload_file(
                str(path), BUCKET, key,
                Config=TransferConfig(multipart_threshold=64 * 1024 * 1024),
            )
            completed[relative] = version
            LOG.info("Uploaded %s (%d bytes) -> hf://%s/%s",
                     relative, stat.st_size, BUCKET, key)
        except Exception as exc:
            LOG.warning("Upload failed for %s: %s: %s",
                        relative, type(exc).__name__, str(exc)[:250])
    for key in list(observed):
        if key not in found:
            observed.pop(key, None)
            completed.pop(key, None)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    s3 = client()
    LOG.info("Syncing primary R2 directory %s -> HF bucket %s, prefix %s",
             SOURCE, BUCKET, PREFIX)
    observed, completed = {}, {}
    while RUNNING:
        try:
            sync_once(s3, observed, completed)
        except Exception as exc:
            LOG.warning("Scan failed: %s: %s", type(exc).__name__, str(exc)[:250])
        for _ in range(POLL):
            if not RUNNING:
                break
            time.sleep(1)


if __name__ == "__main__":
    main()
