"""Archive ComfyUI outputs in HF; keep only small previews in CF R2."""
import logging
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

ROOT = Path(os.getenv("COMFY_OUTPUT_ROOT", "/outputs"))
WORKFLOW = os.getenv("WORKFLOW", "default")
PREVIEWS = Path(os.getenv("COMFY_R2_PREVIEWS", "/r2-previews"))
HF_BUCKET = os.getenv("HF_S3_BUCKET", "comfyui-assets")
INTERVAL = int(os.getenv("HF_SYNC_INTERVAL", "15"))
EXTENSIONS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
              ".mp4", ".webm", ".mov", ".mkv", ".png", ".jpg", ".jpeg",
              ".json", ".bin", ".obj", ".mtl", ".stl"}
VIDEOS = {".mp4", ".webm", ".mov", ".mkv"}
IMAGES = {".png", ".jpg", ".jpeg"}
CATEGORIES = {"3d", "3dgs", "video", "image"}
LOG = logging.getLogger("hf-archive")


def hf_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("HF_S3_ENDPOINT", "https://s3.hf.co/seachen"),
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 5, "mode": "standard"},
        ),
    )


def sha256_file(path):
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def same_content(client, bucket, key, size, digest):
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        return (head["ContentLength"] == size
                and head.get("Metadata", {}).get("sha256") == digest)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def make_video_preview(video):
    preview = video.with_name(video.stem + "_preview.png")
    if preview.exists():
        return
    temporary = video.with_name(video.stem + "_preview.part.png")
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        subprocess.run(
            [get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(video), "-frames:v", "1", str(temporary)],
            capture_output=True, check=True, timeout=90,
        )
        temporary.replace(preview)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        LOG.warning("Video preview failed %s: %s", video.name, exc)


def iter_outputs():
    """Return category-first paths for single and dual ComfyUI instances."""
    if not ROOT.is_dir():
        return
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        parts = path.relative_to(ROOT).parts
        instance = parts[0] if parts[0] in {"instance-1", "instance-2"} else ""
        if instance:
            parts = parts[1:]
        if not parts:
            continue
        if parts[0] in CATEGORIES:
            category, parts = parts[0], parts[1:]
        elif path.suffix.lower() in IMAGES:
            category = "image"
        elif path.suffix.lower() in VIDEOS:
            category = "video"
        elif path.suffix.lower() in {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
                                      ".obj", ".mtl", ".stl"}:
            category = "3d"
        else:
            continue  # Keep uncategorized metadata out of the archive.
        if not parts:
            continue
        relative = "/".join((category, *((instance,) if instance else ()), *parts))
        yield relative, path


def archive_once(hf, observed, completed, now=None):
    """Only upload files unchanged for one interval; retry on future scans."""
    now = time.monotonic() if now is None else now
    present = set()
    for relative, path in iter_outputs():
        present.add(relative)
        stat = path.stat()
        version = (stat.st_size, stat.st_mtime_ns)
        if not stat.st_size or completed.get(relative) == version:
            continue
        previous = observed.get(relative)
        if previous is None or previous[0] != version:
            observed[relative] = (version, now)
            continue
        if now - previous[1] < INTERVAL:
            continue
        key = f"comfyui/{WORKFLOW}/{relative}"
        try:
            digest = sha256_file(path)
            if not same_content(hf, HF_BUCKET, key, stat.st_size, digest):
                hf.upload_file(
                    str(path), HF_BUCKET, key,
                    ExtraArgs={"Metadata": {"sha256": digest}},
                    Config=TransferConfig(multipart_threshold=64 * 1024 * 1024),
                )
            if not same_content(hf, HF_BUCKET, key, stat.st_size, digest):
                raise RuntimeError("HF upload verification failed")

            if path.suffix.lower() in IMAGES:
                target = PREVIEWS / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size != stat.st_size:
                    shutil.copyfile(path, target)
                if target.stat().st_size != stat.st_size:
                    raise RuntimeError("R2 preview verification failed")

            completed[relative] = version
            LOG.info("Archived %s", key)
            if path.suffix.lower() in VIDEOS:
                make_video_preview(path)
        except Exception as exc:
            LOG.warning("Will retry %s: %s", relative, exc)
    for key in list(observed):
        if key not in present:
            observed.pop(key, None)
            completed.pop(key, None)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    hf = hf_client()
    observed, completed = {}, {}
    LOG.info("Syncing %s to HF, small previews to CF R2", ROOT)
    while True:
        try:
            archive_once(hf, observed, completed)
        except Exception:
            LOG.exception("Archive scan failed; retrying")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
