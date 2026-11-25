"""Small, explicit runtime policy and lifecycle helpers for Modal ComfyUI."""
import json
import os
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class RuntimePolicy:
    profile: str
    snapshot: str
    scaledown_window: int
    warmup: str

    @property
    def gpu_snapshot(self):
        return self.snapshot == "gpu"


def resolve_policy(workflow, config, env=None):
    """Keep interactive defaults; batch is an explicit cost-sensitive profile."""
    env = os.environ if env is None else env
    settings = config.get("runtime", {})
    if not isinstance(settings, dict):
        raise ValueError("runtime must be an object")
    unknown = set(settings) - {"profile", "snapshot", "scaledown_window", "warmup"}
    if unknown:
        raise ValueError(f"Unknown runtime options: {sorted(unknown)}")
    profile = env.get("COMFYUI_RUNTIME_PROFILE", settings.get("profile", "interactive"))
    if profile not in ("interactive", "batch"):
        raise ValueError("COMFYUI_RUNTIME_PROFILE must be interactive or batch")
    snapshot = env.get("COMFYUI_SNAPSHOT_MODE", settings.get("snapshot", "gpu"))
    if snapshot not in ("gpu", "off"):
        raise ValueError("COMFYUI_SNAPSHOT_MODE must be gpu or off")
    default_window = 60 if profile == "batch" else 300 if workflow == "comfyui-idol" else 1800
    raw = env.get("COMFYUI_SCALEDOWN_SECONDS", settings.get("scaledown_window", default_window))
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("scaledown_window must be an integer")
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError("scaledown_window must be an integer") from exc
    if not 2 <= seconds <= 1800:
        raise ValueError("scaledown_window must be within 2..1800 seconds")
    default_warmup = "h3-int8" if workflow == "010-minimax-h3-b200plus-suite" else "off"
    if env.get("SNAPSHOT_H3_WARMUP") == "0":
        default_warmup = "off"
    warmup = env.get("COMFYUI_WARMUP_MODE", settings.get("warmup", default_warmup))
    if warmup not in ("off", "h3-int8"):
        raise ValueError("COMFYUI_WARMUP_MODE must be off or h3-int8")
    if warmup == "h3-int8" and workflow != "010-minimax-h3-b200plus-suite":
        raise ValueError("The bundled warmup graph is only valid for the INT8 H3 suite")
    return RuntimePolicy(profile, snapshot, seconds, warmup)


def log_event(event, **fields):
    print(json.dumps({"event": event, "container_id": os.getenv("MODAL_TASK_ID"),
                      "time": time.time(), **fields}, ensure_ascii=False), flush=True)


def wait_for_server(port, proc, timeout):
    """Check actual ComfyUI HTTP readiness, not merely an open socket."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise RuntimeError(f"ComfyUI port {port} exited with code {code}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/system_stats",
                                        timeout=min(2, max(0.1, deadline - time.monotonic()))) as response:
                data = json.load(response)
            if isinstance(data, dict) and "system" in data and "devices" in data:
                return
            last = "unexpected system_stats response"
        except (OSError, ValueError) as exc:
            last = str(exc)
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"ComfyUI port {port} not HTTP-ready: {last}")


def stop_process(proc, label, timeout=10):
    """The launch CLI can spawn children. Terminate its isolated POSIX group."""
    started = time.monotonic()
    forced = False
    try:
        if os.name == "posix":
            # Popen(start_new_session=True) guarantees a dedicated group.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            forced = True
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            proc.wait(timeout=5)
        if os.name == "posix":
            # The leader can exit before its descendants. A remaining group
            # must not continue writing files during the final Volume commit.
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                pass
            else:
                forced = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # Confirm the group disappeared before permitting a final
                # commit; an unconfirmed exit is logged and deferred.
                deadline = time.monotonic() + 5
                while True:
                    try:
                        os.killpg(proc.pid, 0)
                    except ProcessLookupError:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"Process group {proc.pid} still present after SIGKILL")
                    time.sleep(0.1)
    finally:
        log_event("process_stopped", process=label, pid=proc.pid,
                  returncode=proc.poll(), forced=forced, seconds=time.monotonic()-started)
