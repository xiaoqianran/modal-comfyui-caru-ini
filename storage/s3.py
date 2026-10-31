"""HF is the archive. R2 holds previews and temporary download copies."""
import os
import tempfile
from pathlib import Path
from urllib.parse import unquote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

EXTENSIONS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz",
              ".mp4", ".webm", ".mov", ".mkv", ".png", ".jpg", ".jpeg",
              ".json", ".bin", ".obj", ".mtl", ".stl"}
CATEGORIES = ("3d", "3dgs", "video", "image")


def client(source):
    if source == "r2":
        endpoint = os.getenv("R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com")
        bucket = os.getenv("R2_BUCKET", "modal-studio-assets")
        access = os.environ["AWS_ACCESS_KEY_ID"]
        secret = os.environ["AWS_SECRET_ACCESS_KEY"]
        region = "auto"
    elif source == "hf":
        endpoint = os.getenv("HF_S3_ENDPOINT", "https://s3.hf.co/seachen")
        bucket = os.getenv("HF_S3_BUCKET", "comfyui-assets")
        access = os.environ["HF_S3_ACCESS_KEY_ID"]
        secret = os.environ["HF_S3_SECRET_ACCESS_KEY"]
        region = "us-east-1"
    else:
        raise ValueError("Invalid storage source")
    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access,
        aws_secret_access_key=secret, region_name=region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    ), bucket


def check_key(key, source="hf"):
    key = unquote(key)
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in key:
        raise ValueError("Invalid asset key")
    if source == "hf":
        valid = len(parts) >= 4 and parts[0] == "comfyui" and parts[2] in CATEGORIES
    else:
        valid = (len(parts) >= 4 and parts[0] == "previews" and parts[2] in CATEGORIES) or (
            len(parts) >= 5 and parts[0:2] == ["downloads", "comfyui"] and parts[3] in CATEGORIES
        ) or (len(parts) >= 4 and parts[0] == "comfyui" and parts[2] in CATEGORIES)
    if not valid or Path(key).suffix.lower() not in EXTENSIONS:
        raise ValueError("Invalid asset key")
    return key


def list_assets(source, workflow=None):
    s3, bucket = client(source)
    # Keep old CF data visible while new files are permanently archived on HF.
    prefixes = ["comfyui/"] if source == "hf" else ["previews/", "comfyui/"]
    result = []
    for base in prefixes:
        prefix = base + (workflow + "/" if workflow else "")
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                try:
                    check_key(key, source)
                except ValueError:
                    continue
                result.append({"source": source, "key": key, "size": obj["Size"]})
    return result


def get_asset(source, key):
    key = check_key(key, source)
    s3, bucket = client(source)
    return s3.get_object(Bucket=bucket, Key=key)


def maybe_head(s3, bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def download_via_r2(key):
    """PC2 transfers an HF original to CF only when the user requests it."""
    key = check_key(key, "hf")
    hf, hf_bucket = client("hf")
    r2, r2_bucket = client("r2")
    source = hf.head_object(Bucket=hf_bucket, Key=key)
    size = source["ContentLength"]
    etag = source.get("ETag", "").strip('"')
    r2_key = "downloads/" + key
    existing = maybe_head(r2, r2_bucket, r2_key)
    reused = bool(
        existing and existing["ContentLength"] == size
        and existing.get("Metadata", {}).get("hf-etag") == etag
    )
    if not reused:
        with tempfile.TemporaryDirectory(prefix="hf-to-r2-") as folder:
            path = str(Path(folder) / Path(key).name)
            hf.download_file(hf_bucket, key, path)
            if os.path.getsize(path) != size:
                raise RuntimeError("HF download size mismatch")
            r2.upload_file(path, r2_bucket, r2_key, ExtraArgs={"Metadata": {"hf-etag": etag}})
        confirmed = r2.head_object(Bucket=r2_bucket, Key=r2_key)
        if confirmed["ContentLength"] != size:
            raise RuntimeError("R2 upload size mismatch")
    url = r2.generate_presigned_url(
        "get_object",
        Params={"Bucket": r2_bucket, "Key": r2_key},
        ExpiresIn=3600,
    )
    return {"url": url, "reused": reused, "size": size}
