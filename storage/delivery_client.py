"""Local authenticated client for optional CPU/Volume task delivery."""
from __future__ import annotations

import time
import threading

from storage.delivery import normalized_config, receipt_file, validate_receipt, verified_copy, volume_receipt


class DeliveryClient:
    def __init__(self, config, base):
        import modal
        self.modal = modal
        self.config = normalized_config(config)
        if "version" not in self.config:
            raise ValueError("Task delivery requires an explicit deployment version")
        self.base = base.rstrip("/")
        self.volume = modal.Volume.from_name(self.config["volume"], version=2)
        self._probe_lock = threading.RLock()
        self._probe_identity = None
        self._probe_info = None
        self._verified_model = None

    def _model(self):
        model = getattr(self, "_verified_model", None)
        return model if model is not None else self.modal.Cls.from_name(self.config["app"], "ComfyUI")()

    def preflight(self):
        """Check the current deployment every time; reuse proof only for the same lifecycle."""
        with self._probe_lock:
            return self._preflight_locked()

    def _preflight_locked(self):
        cfg = self.config
        name = "delivery_info_" + cfg["version"][:32]
        info = self.modal.App.lookup(cfg["app"]).info()
        version = info.lifecycle.version
        identity = (info.app_id, version, tuple(sorted(cfg.items())), self.base)
        if name not in info.functions:
            self._probe_identity = self._probe_info = self._verified_model = None
            raise RuntimeError("Expected ComfyUI delivery version is not deployed")
        if (version is not None and identity == self._probe_identity
                and self._probe_info is not None):
            return dict(self._probe_info)
        # A redeploy or changed target invalidates both proof and hydrated model.
        self._probe_identity = self._probe_info = self._verified_model = None
        deployed = self.modal.Function.from_name(cfg["app"], name).remote()
        expected = {k: cfg[k] for k in ("app", "workflow", "volume", "version")}
        if any(deployed.get(k) != value for k, value in expected.items()):
            raise RuntimeError("ComfyUI delivery probe identity mismatch")
        if deployed.get("delivery") != "cpu" or cfg["instance"] > deployed.get("instance_count", 0):
            raise RuntimeError("ComfyUI delivery mode or instance mismatch")
        # Resolving a function's web URL is metadata hydration, not GPU execution.
        model = self._model()
        route = model.ui if cfg["instance"] == 1 else model.ui_second
        url = route.get_web_url()
        if not url or url.rstrip("/") != self.base:
            raise RuntimeError("ComfyUI endpoint URL does not match authenticated deployment")
        current = self.modal.App.lookup(cfg["app"]).info()
        if current.app_id != info.app_id or current.lifecycle.version != info.lifecycle.version or name not in current.functions:
            raise RuntimeError("ComfyUI deployment changed during preflight")
        if version is not None:
            self._probe_identity = identity
            self._probe_info = dict(deployed)
            self._verified_model = model
        return dict(deployed)

    def receipt(self, prompt_id):
        # Successful prior publication requires only Volume access.
        ready = volume_receipt(self.volume, self.config, prompt_id)
        if ready is not None:
            return ready
        self.preflight()
        self._model().publish_outputs.remote(prompt_id, self.config["instance"], self.config["version"])
        # Poll the Volume, not the GPU, if marker visibility is briefly delayed.
        for attempt in range(3):
            ready = volume_receipt(self.volume, self.config, prompt_id)
            if ready is not None:
                return validate_receipt(ready, self.config, prompt_id)
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("Publisher returned before committed receipt became visible")

    def download(self, receipt, item, target, deadline=lambda: None):
        file = receipt_file(receipt, item)
        return verified_copy(self.volume.read_file(file["key"]), target, file, deadline)
