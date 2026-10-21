"""Archive ComfyUI outputs in HF; keep only small previews in CF R2."""
import logging
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
SOURCES = {name: ROOT / name for name in ("3d", "3dgs", "video")}
PREVIEWS = Path(os.getenv("COMFY_R2_PREVIEWS", "/r2-previews"))
HF_BUCKET = os.getenv("HF_S3_BUCKET", "comfyui-assets")
INTERVAL = int(os.getenv("HF_SYNC_INTERVAL", "15"))
EXTENSIONS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
              ".mp4", ".webm", ".mov", ".mkv", ".png", ".jpg", ".jpeg",
              ".json", ".bin", ".obj", ".mtl", ".stl"}
VIDEOS = {".mp4", ".webm", ".mov", ".mkv"}
IMAGES = {".png", ".jpg", ".jpeg"}
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


def same_size(client, bucket, key, size):
    try:
        return client.head_object(Bucket=bucket, Key=key)["ContentLength"] == size
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


def archive_once(hf, observed, completed, now=None):
    """Only upload files unchanged for one interval; retry on future scans."""
    now = time.monotonic() if now is None else now
    present = set()
    for category, directory in SOURCES.items():
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
                continue
            relative = f"{category}/{path.relative_to(directory).as_posix()}"
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
                if not same_size(hf, HF_BUCKET, key, stat.st_size):
                    hf.upload_file(str(path), HF_BUCKET, key,
                                   Config=TransferConfig(multipart_threshold=64 * 1024 * 1024))
                if not same_size(hf, HF_BUCKET, key, stat.st_size):
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
    LOG.info("Syncing %s to HF, small previews to CF R2", SOURCES)
    while True:
        try:
            archive_once(hf, observed, completed)
        except Exception:
            LOG.exception("Archive scan failed; retrying")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
