"""Single-writer CPU archive consumer for committed prompt receipts."""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from storage.delivery import READY, archive_key, atomic_json, checked_source, validate_receipt, verified_copy
from storage.output_formats import IMAGES, VIDEOS

LOG = logging.getLogger("delivery-archive")


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def receipt_config(receipt):
    cfg = {k: receipt[k] for k in ("app", "workflow", "instance", "volume")}
    if receipt.get("version") is not None:
        cfg["version"] = receipt["version"]
    return cfg


def iter_chunks(path):
    with path.open("rb") as stream:
        yield from iter(lambda: stream.read(1024 * 1024), b"")


def _head_size(client, bucket, key):
    from botocore.exceptions import ClientError
    try:
        return client.head_object(Bucket=bucket, Key=key)["ContentLength"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def _preview(local, key, previews):
    if previews is None:
        return
    from storage.hf_sync import make_video_preview
    from shutil import copyfile

    preview = local
    if local.suffix.lower() in VIDEOS:
        make_video_preview(local)
        preview = local.with_name(local.stem + "_preview.png")
        key = str(Path(key).with_suffix("")) + "_preview.png"
        key = key.replace("\\", "/")
    elif local.suffix.lower() not in IMAGES:
        return
    target = previews.joinpath(*key.removeprefix("comfyui/").split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    # CloudBucketMount is an object-store mount, not a POSIX filesystem:
    # rename is unsupported. This key is prompt-isolated; publish by closing
    # the object write, verify length, and only then persist the preview receipt.
    copyfile(preview, target)
    if target.stat().st_size != preview.stat().st_size:
        raise RuntimeError("Preview size mismatch")


def archive_receipt(source, receipt, state, persist, hf, bucket, previews=None):
    """Copy/hash on CPU before upload; persist each completed transfer."""
    validate_receipt(receipt, receipt_config(receipt), receipt["prompt_id"])
    for file in receipt["files"]:
        key = archive_key(receipt, file)
        record = state.setdefault("files", {}).setdefault(file["key"], {})
        identity = {"key": key, "bytes": file["bytes"], "sha256": file["sha256"]}
        if record.get("identity") not in (None, identity):
            raise RuntimeError("Archive ledger identity conflict")
        if (record.get("identity") == identity and record.get("archived") and record.get("preview")
                and _head_size(hf, bucket, key) == file["bytes"]):
            continue
        with tempfile.TemporaryDirectory(prefix="comfy-delivery-") as directory:
            local = Path(directory) / file["filename"]
            verified_copy(iter_chunks(checked_source(source, file["key"])), local, file)
            if (record.get("identity") != identity or not record.get("archived")
                    or _head_size(hf, bucket, key) != file["bytes"]):
                # Size alone never authorizes skipping a new receipt.
                hf.upload_file(str(local), bucket, key)
                if _head_size(hf, bucket, key) != file["bytes"]:
                    raise RuntimeError("HF archive size mismatch")
                record.update(identity=identity, archived=True, preview=False)
                persist()
            _preview(local, key, previews)
            record["preview"] = True
            persist()
    state.update(status="completed", error=None, next_retry_at=0)
    persist()


def archive_pending(source, state_root, state_volume, hf, bucket, previews=None, limit=8,
                    now=None, expected_volume=None):
    """Retry durable ready receipts, skip completed or backoff entries.

    The caller guarantees a single writer per state volume.
    """
    if not 1 <= limit <= 32:
        raise ValueError("limit must be within 1..32")
    now = time.time() if now is None else now
    ready = source / READY
    result = {"processed": 0, "completed": 0, "failed": 0}
    if not ready.is_dir():
        return result
    for path in sorted(ready.glob("*/*/*.json")):
        if result["processed"] >= limit:
            break
        relative = path.relative_to(ready)
        if path.is_symlink() or any(p.is_symlink() for p in (path.parent, path.parent.parent)):
            continue
        target = state_root / "archive" / relative
        try:
            state = _read_json(target) if target.is_file() else {}
            if not isinstance(state, dict):
                raise ValueError("Archive ledger must be an object")
        except (OSError, ValueError) as exc:
            # Preserve the original damaged ledger for operator inspection;
            # it must not prevent unrelated prompt receipts from progressing.
            LOG.warning("Archive ledger unreadable %s: %s", relative, exc)
            result["processed"] += 1
            result["failed"] += 1
            continue
        if state.get("status") == "completed" or state.get("next_retry_at", 0) > now:
            continue
        result["processed"] += 1

        def persist():
            atomic_json(target, state)
            state_volume.commit()

        try:
            receipt = _read_json(path)
            validate_receipt(receipt, receipt_config(receipt), receipt["prompt_id"])
            expected = (receipt["workflow"], str(receipt["instance"]), receipt["prompt_id"] + ".json")
            if relative.parts != expected or (expected_volume and receipt["volume"] != expected_volume):
                raise ValueError("Ready marker path/volume identity mismatch")
            state["status"] = "archiving"
            archive_receipt(source, receipt, state, persist, hf, bucket, previews)
            result["completed"] += 1
        except Exception as exc:
            attempts = state.get("attempts", 0) + 1
            state.update(status="retry", attempts=attempts,
                         next_retry_at=now + min(3600, 30 * 2 ** min(attempts, 7)),
                         error=str(exc)[:500])
            persist()
            LOG.warning("Archive retry %s: %s", relative, exc)
            result["failed"] += 1
    return result
