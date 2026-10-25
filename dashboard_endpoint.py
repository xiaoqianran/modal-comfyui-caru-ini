"""Lightweight browser entrypoint for the original Modal ComfyUI Class Web URL.

The GPU-backed ComfyUI.ui is unchanged. This CPU-only endpoint is registered
as a top-level Web Function so Modal Dashboard exposes an obvious Web URL.
"""
from __future__ import annotations

import os


def open_comfyui():
    import modal
    from fastapi.responses import RedirectResponse

    app_name = os.environ["COMFYUI_APP_NAME"]
    url = modal.Cls.from_name(app_name, "ComfyUI")().ui.get_web_url()
    if not url or not url.startswith("https://"):
        raise RuntimeError(f"No deployed ComfyUI.ui HTTPS URL for {app_name}")
    return RedirectResponse(url=url, status_code=307)