"""Upload generated ComfyUI artifacts to Cloudflare R2."""
import mimetypes
import os
from pathlib import Path


def upload(file: str | Path, key: str) -> str:
    import boto3

    path = Path(file)
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    client.upload_file(
        str(path),
        os.environ.get("R2_BUCKET", "modal-studio-assets"),
        key,
        ExtraArgs={"ContentType": mimetypes.guess_type(path.name)[0] or "application/octet-stream"},
    )
    return key
