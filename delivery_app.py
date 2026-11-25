"""Optional CPU-only archival service. Deploy separately from ComfyUI."""
import os
import tempfile
from pathlib import Path

import modal

from storage.delivery import DEFAULT_VOLUME, checked_source, token, validate_receipt, verified_copy, volume_receipt
from storage.delivery_archive import archive_pending as consume, iter_chunks
from storage.hf_sync import hf_client

OUTPUT_NAME = token(os.getenv("COMFYUI_OUTPUT_VOLUME_NAME", DEFAULT_VOLUME), "volume")
STATE_NAME = token(os.getenv("COMFYUI_DELIVERY_STATE_VOLUME", "modal-comfyui-delivery-state"), "state volume")
APP_NAME = token(os.getenv("COMFYUI_DELIVERY_APP", "modal-comfyui-delivery"), "archive app")
if OUTPUT_NAME == STATE_NAME:
    raise ValueError("Delivery state and output must use separate volumes")
SCHEDULE = int(os.getenv("COMFYUI_DELIVERY_SCHEDULE_MINUTES", "0"))
if SCHEDULE < 0 or SCHEDULE > 60:
    raise ValueError("Schedule minutes must be within 0..60 (0 disables)")
PREVIEWS = os.getenv("COMFYUI_DELIVERY_PREVIEWS", "1") == "1"

output = modal.Volume.from_name(OUTPUT_NAME, version=2)
state = modal.Volume.from_name(STATE_NAME, version=2, create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.11")
    .uv_pip_install("boto3", "imageio-ffmpeg")
    .env({"COMFYUI_OUTPUT_VOLUME_NAME": OUTPUT_NAME,
          "COMFYUI_DELIVERY_STATE_VOLUME": STATE_NAME, "COMFYUI_DELIVERY_APP": APP_NAME,
          "COMFYUI_DELIVERY_SCHEDULE_MINUTES": str(SCHEDULE),
          "COMFYUI_DELIVERY_PREVIEWS": "1" if PREVIEWS else "0"})
    .add_local_python_source("storage.delivery", "storage.delivery_archive",
                             "storage.output_formats", "storage.hf_sync")
)
app = modal.App(APP_NAME, image=image)
mounts = {"/source": output.with_mount_options(read_only=True), "/state": state}
if PREVIEWS:
    mounts["/previews"] = modal.CloudBucketMount(
        bucket_name="modal-studio-assets",
        bucket_endpoint_url=os.getenv("R2_ENDPOINT", "https://f863e42ff2096d089d4d4129197abd17.r2.cloudflarestorage.com"),
        key_prefix="previews/",
        secret=modal.Secret.from_name("cloudflare-r2"),
    )


@app.function(cpu=1, memory=2048, timeout=1800, startup_timeout=300,
              volumes=mounts, secrets=[modal.Secret.from_name("huggingface-s3")],
              max_containers=1, min_containers=0, scaledown_window=10,
              schedule=modal.Period(minutes=SCHEDULE) if SCHEDULE else None)
@modal.concurrent(max_inputs=1)
def archive_pending():
    output.reload()
    state.reload()
    return consume(Path("/source"), Path("/state"), state, hf_client(),
                   os.getenv("HF_S3_BUCKET", "comfyui-assets"),
                   Path("/previews") if PREVIEWS else None,
                   expected_volume=OUTPUT_NAME)


@app.function(cpu=0.5, memory=512, timeout=300, volumes={"/source": output.with_mount_options(read_only=True)},
              max_containers=1, min_containers=0, scaledown_window=5)
def verify_delivery(config: dict, prompt_id: str):
    """Authenticated CPU probe; validates exact committed bytes, never wakes GPU."""
    if config.get("volume", DEFAULT_VOLUME) != OUTPUT_NAME:
        raise ValueError("Wrong output volume")
    output.reload()
    receipt = volume_receipt(output, config, prompt_id)
    if receipt is None:
        raise FileNotFoundError("Receipt not committed")
    validate_receipt(receipt, config, prompt_id)
    with tempfile.TemporaryDirectory() as directory:
        for i, file in enumerate(receipt["files"]):
            verified_copy(iter_chunks(checked_source(Path("/source"), file["key"])),
                          Path(directory) / str(i), file)
    return {"status": "VERIFIED", "receipt": receipt,
            "container_id": os.getenv("MODAL_TASK_ID")}
