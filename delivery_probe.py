"""Version-specific CPU identity probe, independent of GPU image/engine."""
import os


def deployment_info():
    return {"app": os.environ["COMFYUI_APP_NAME"], "workflow": os.environ["WORKFLOW"],
            "volume": os.environ["COMFYUI_OUTPUT_VOLUME_NAME"],
            "version": os.environ["COMFYUI_DEPLOYMENT_VERSION"],
            "instance_count": int(os.environ["COMFYUI_INSTANCE_COUNT"]),
            "delivery": "cpu", "execution_schema": 1}
