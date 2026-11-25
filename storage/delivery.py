"""Authenticated, content-verified handoff from ComfyUI to Volume/CPU workers.

No public download endpoint, no GPU invocation when a ready receipt exists.
The original output is not duplicated; a changed source fails closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

from storage.output_formats import EXTENSIONS, CATEGORIES, IMAGES, VIDEOS, AUDIO, MODELS

DEFAULT_VOLUME = "modal-comfyui-models-output"
READY = ".delivery/ready"
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
MAX_RECEIPT_BYTES = 4 * 1024 * 1024


def token(value, label="identifier"):
    if not isinstance(value, str) or not TOKEN.fullmatch(value) or value in (".", ".."):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Unsafe delivery path")
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.startswith(".delivery") for part in parts):
        raise ValueError("Unsafe delivery path")
    return PurePosixPath(value)


def output_ref(item):
    filename = item["filename"]
    if not isinstance(filename, str) or "/" in filename or "\\" in filename:
        raise ValueError("Expected a plain ComfyUI output filename")
    subfolder = item.get("subfolder", "")
    if not isinstance(subfolder, str):
        raise ValueError("Invalid output subfolder")
    path = relative_path(f"{subfolder}/{filename}" if subfolder else filename)
    if item.get("type", "output") != "output":
        raise ValueError("Only persisted ComfyUI output files can use CPU delivery")
    if path.suffix.lower() not in EXTENSIONS or any(
        part == "__snapshot_warmup__" or ".part." in part.lower() or part.lower().endswith(".part")
        for part in path.parts
    ):
        raise ValueError(f"Unsupported or temporary output: {path}")
    return path


def instance_number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value not in (1, 2):
        raise ValueError("instance must be 1 or 2")
    return value


def source_key(workflow, instance, dual, path):
    base = PurePosixPath(token(workflow, "workflow"))
    instance_number(instance)
    if dual:
        base /= f"instance-{instance}"
    elif instance != 1:
        raise ValueError("Single-server workflow requires instance=1")
    return str(base / relative_path(str(path)))


def receipt_key(workflow, instance, prompt_id):
    return f"{READY}/{token(workflow, 'workflow')}/{instance_number(instance)}/{token(prompt_id, 'prompt ID')}.json"


def normalized_config(config):
    if not isinstance(config, dict):
        raise ValueError("delivery must be an object")
    if set(config) - {"app", "workflow", "instance", "volume", "version"}:
        raise ValueError("Unknown delivery options")
    if "app" not in config or "workflow" not in config:
        raise ValueError("delivery requires app and workflow")
    result = {
        "app": token(config["app"], "app"),
        "workflow": token(config["workflow"], "workflow"),
        "instance": instance_number(config.get("instance", 1)),
        "volume": token(config.get("volume", DEFAULT_VOLUME), "volume"),
    }
    version = config.get("version")
    if version is not None:
        if not isinstance(version, str) or not HEX.fullmatch(version):
            raise ValueError("delivery.version must be a full SHA-256")
        result["version"] = version
    return result


def file_digest(path):
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    if (size <= 0 or size != before.st_size or
        (before.st_size, before.st_mtime_ns, before.st_ino) !=
        (after.st_size, after.st_mtime_ns, after.st_ino)):
        raise RuntimeError(f"Output changed during publication: {path.name}")
    return {"bytes": size, "sha256": digest.hexdigest()}


def checked_source(root, key):
    path = root.joinpath(*relative_path(key).parts)
    # Refuse links at every component, even links staying within the volume.
    current = root
    for part in relative_path(key).parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink is not a deliverable: {key}")
    if not path.is_file():
        raise FileNotFoundError(key)
    return path


def build_receipt(root, config, prompt_id, history, dual=False, version=None):
    """Called only after native history confirms successful completion."""
    config = normalized_config(config)
    token(prompt_id, "prompt ID")
    status = history.get("status", {})
    if status.get("status_str") != "success" or status.get("completed") is not True:
        raise RuntimeError("ComfyUI prompt is not successfully completed")
    if version is not None and (not isinstance(version, str) or not HEX.fullmatch(version)):
        raise ValueError("Invalid deployment version")
    if config.get("version") is not None and config["version"] != version:
        raise RuntimeError("Deployment version changed")
    files = {}
    for node in history.get("outputs", {}).values():
        for items in node.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or "filename" not in item:
                    continue
                if item.get("type", "output") != "output":
                    continue
                relative = output_ref(item)
                key = source_key(config["workflow"], config["instance"], dual, relative)
                if key not in files:
                    files[key] = {"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                  "key": key, **file_digest(checked_source(root, key))}
    if not files:
        raise RuntimeError("No persisted output files in successful history")
    return {"schema": 1, **config, "version": version, "prompt_id": prompt_id,
            "dual": bool(dual), "files": list(files.values())}


def validate_receipt(receipt, config, prompt_id):
    cfg = normalized_config(config)
    if not isinstance(receipt, dict) or receipt.get("schema") != 1:
        raise ValueError("Unsupported delivery receipt")
    for key, value in {**cfg, "prompt_id": token(prompt_id, "prompt ID")}.items():
        if receipt.get(key) != value:
            raise ValueError(f"Receipt identity mismatch: {key}")
    if type(receipt.get("dual")) is not bool:
        raise ValueError("Invalid receipt instance layout")
    version = receipt.get("version")
    if version is not None and (not isinstance(version, str) or not HEX.fullmatch(version)):
        raise ValueError("Invalid receipt version")
    files = receipt.get("files")
    if not isinstance(files, list) or not files or len(files) > 10000:
        raise ValueError("Invalid receipt files")
    seen = set()
    for file in files:
        if not isinstance(file, dict):
            raise ValueError("Invalid receipt file")
        expected = source_key(cfg["workflow"], cfg["instance"], receipt["dual"], output_ref(file))
        if file.get("key") != expected or expected in seen:
            raise ValueError("Invalid or duplicate receipt source key")
        if (type(file.get("bytes")) is not int or file["bytes"] <= 0
                or not isinstance(file.get("sha256"), str) or not HEX.fullmatch(file["sha256"])):
            raise ValueError("Invalid receipt file evidence")
        seen.add(expected)
    return receipt


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(raw) > MAX_RECEIPT_BYTES:
            raise ValueError("Receipt too large")
        temporary.write_bytes(raw)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_receipt(root, volume, receipt):
    """Caller serializes all commits. Data is durable before ready marker."""
    config = {k: receipt[k] for k in ("app", "workflow", "instance", "volume")}
    if receipt.get("version") is not None:
        config["version"] = receipt["version"]
    validate_receipt(receipt, config, receipt["prompt_id"])
    path = root / receipt_key(receipt["workflow"], receipt["instance"], receipt["prompt_id"])
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != receipt:
            raise RuntimeError("Conflicting immutable prompt receipt")
        # A prior receipt commit could have raised after writing locally.
        volume.commit()
        return existing
    volume.commit()
    atomic_json(path, receipt)
    volume.commit()
    return receipt


def volume_receipt(volume, config, prompt_id):
    """Read committed marker without starting a GPU. Missing marker is distinct."""
    key = receipt_key(config["workflow"], config["instance"], prompt_id)
    data = bytearray()
    try:
        for chunk in volume.read_file(key):
            data.extend(chunk)
            if len(data) > MAX_RECEIPT_BYTES:
                raise ValueError("Receipt too large")
    except FileNotFoundError:
        return None
    return validate_receipt(json.loads(data), config, prompt_id)


def receipt_file(receipt, item):
    relative = output_ref(item)
    key = source_key(receipt["workflow"], receipt["instance"], receipt["dual"], relative)
    matches = [file for file in receipt["files"] if file["key"] == key]
    if len(matches) != 1:
        raise ValueError(f"Output absent from delivery receipt: {key}")
    return matches[0]


def verified_copy(chunks, destination, expected, check_deadline=lambda: None):
    """Stream once, check source digest/length, publish atomically."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".part")
    size, digest = 0, hashlib.sha256()
    try:
        with temporary.open("xb") as stream:
            for chunk in chunks:
                check_deadline()
                size += len(chunk)
                if size > expected["bytes"]:
                    raise RuntimeError("Delivery exceeded declared size")
                digest.update(chunk)
                stream.write(chunk)
        check_deadline()
        if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise RuntimeError("Delivery SHA-256/size mismatch")
        os.replace(temporary, destination)
        return {"bytes": size, "sha256": digest.hexdigest()}
    finally:
        temporary.unlink(missing_ok=True)


def archive_key(receipt, file):
    """Prompt-isolated names remain compatible with category-first asset browser."""
    path = output_ref(file)
    parts = list(path.parts)
    if parts[0] in CATEGORIES and len(parts) > 1:
        category = parts[0]
    elif path.suffix.lower() in IMAGES:
        category = "image"
    elif path.suffix.lower() in VIDEOS:
        category = "video"
    elif path.suffix.lower() in AUDIO:
        category = "audio"
    elif path.suffix.lower() in MODELS:
        category = "3d"
    else:
        category = "3d" if path.suffix.lower() in {".json", ".bin"} else None
        if category is None:
            raise ValueError("Unknown archive category")
    prefix = ["comfyui", receipt["workflow"], category]
    if receipt["dual"]:
        prefix.append(f"instance-{receipt['instance']}")
    return "/".join(prefix + ["_delivery", receipt["prompt_id"]] + parts)
