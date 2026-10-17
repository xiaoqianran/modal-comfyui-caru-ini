"""Read private Cloudflare R2 and Hugging Face S3 assets with one interface."""
import os
from pathlib import Path
from urllib.parse import unquote

import boto3
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
EXTENSIONS = {".glb", ".gltf", ".ply", ".splat", ".ksplat", ".spz"}

def client(source):
    if source == "r2":
        endpoint = os.getenv("R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com")
        bucket = os.getenv("R2_BUCKET", "modal-studio-assets")
        access = os.environ["AWS_ACCESS_KEY_ID"]
        secret = os.environ["AWS_SECRET_ACCESS_KEY"]
    elif source == "hf":
        endpoint = os.getenv("HF_S3_ENDPOINT", "https://s3.hf.co/seachen")
        bucket = os.getenv("HF_S3_BUCKET", "comfyui-assets")
        access = os.environ["HF_S3_ACCESS_KEY_ID"]
        secret = os.environ["HF_S3_SECRET_ACCESS_KEY"]
    else:
        raise ValueError("Invalid storage source")
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                        aws_secret_access_key=secret, region_name="us-east-1",
                        config=Config(s3={"addressing_style": "path"})), bucket

def check_key(key):
    key = unquote(key)
    if not key.startswith("comfyui/") or ".." in key.split("/") or Path(key).suffix.lower() not in EXTENSIONS:
        raise ValueError("Invalid asset key")
    return key

def list_assets(source, workflow=None):
    s3, bucket = client(source)
    prefix = f"comfyui/{workflow}/3d/" if workflow else "comfyui/"
    result = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        result.extend({"source": source, "key": obj["Key"], "size": obj["Size"]}
                      for obj in page.get("Contents", [])
                      if Path(obj["Key"]).suffix.lower() in EXTENSIONS)
    return result

def get_asset(source, key):
    s3, bucket = client(source)
    return s3.get_object(Bucket=bucket, Key=check_key(key))
