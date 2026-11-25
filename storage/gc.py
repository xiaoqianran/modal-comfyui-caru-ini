"""Conservative offline mark-and-sweep for managed durable media in Output Volume V2.

Every published plan, request and receipt remains a permanent retention root.
Never remove execution metadata, claims, successful outputs, or unknown files.
Actual deletion requires an externally enforced, exclusive maintenance window.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import PurePosixPath

from storage import delivery as d, execution as e, workflow as w
from storage.output_formats import CATEGORIES


DEFAULT_MIN_AGE_HOURS = 168
PLAN_MAX_AGE_SECONDS = 3600


def _managed(path):
    parts = path.split("/")
    return (parts[0] == ".execution" or path.startswith(d.READY + "/")
            or e.OUTPUT_NAMESPACE in parts)


def _inventory(volume):
    files = {}
    for entry in volume.iterdir("/", recursive=True):
        path = entry.path.removeprefix("/")
        if not _managed(path):
            continue
        if int(entry.type) == 2:  # SDK FileEntryType.DIRECTORY
            continue
        if (int(entry.type) != 1 or not path or "\\" in path or ":" in path
                or any(part in ("", ".", "..") for part in path.split("/"))
                or path in files):
            raise RuntimeError(f"Unsafe or duplicate managed Volume entry: {path}")
        if type(entry.size) is not int or entry.size < 0 or type(entry.mtime) is not int:
            raise RuntimeError(f"Invalid managed file metadata: {path}")
        files[path] = {"key": path, "bytes": entry.size, "mtime": entry.mtime}
    return files


def _record(volume, path):
    value = e._read_json(volume, path)
    if not isinstance(value, dict):
        raise RuntimeError(f"Missing or invalid GC root record: {path}")
    return value


def _config_from_receipt(record):
    return d.normalized_config({key: record[key] for key in (
        "app", "workflow", "instance", "volume", "version")
        if record.get(key) is not None})


def _job_config(parts, volume_name):
    return d.normalized_config({"app": parts[3], "workflow": parts[4],
                                "version": parts[5], "instance": int(parts[6]),
                                "volume": volume_name})


def _protect(protected, key, size):
    if key in protected and protected[key] != size:
        raise RuntimeError(f"Conflicting media size evidence: {key}")
    protected[key] = size


def _protect_receipt(protected, receipt):
    for item in receipt["files"]:
        _protect(protected, item["key"], item["bytes"])


def _protect_input(protected, ref):
    _protect(protected, e.input_key(ref), ref["bytes"])


def _failed_owner(path, owners):
    """Recognize only the exact private output layout of a known failed job."""
    parts = path.split("/")
    if parts.count(e.OUTPUT_NAMESPACE) != 1:
        return None
    idx = parts.index(e.OUTPUT_NAMESPACE)
    if idx + 2 >= len(parts):
        return None
    ident = parts[idx + 1]
    matches = owners.get(ident, [])
    if len(matches) != 1:
        return None
    request, state = matches[0]
    if state != "failed":
        return None
    cfg = request["config"]
    base = [cfg["workflow"]]
    if cfg["instance"] == 1:
        layouts = [base, base + ["instance-1"]]
    else:
        layouts = [base + ["instance-2"]]
    allowed = layouts + [layout + [category] for layout in layouts for category in CATEGORIES]
    if parts[:idx] not in allowed:
        return None
    # Do not delete unknown formats, temporary files, or reserved paths.
    item = {"filename": parts[-1], "subfolder": "/".join(parts[idx:-1]), "type": "output"}
    try:
        d.output_ref(item)
    except (ValueError, KeyError):
        return None
    return ident


def _digest_inventory(files, records):
    # Incremental hashing avoids the execution JSON size limit on large inventories.
    digest = hashlib.sha256()
    for key in sorted(files):
        row = [files[key], e.sha_json(records[key]) if key in records else None]
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    return digest.hexdigest()


def scan(volume, volume_name, *, min_age_hours=DEFAULT_MIN_AGE_HOURS, now=None):
    """Read metadata roots, trace dependencies, and produce a non-mutating GC plan."""
    d.token(volume_name, "Output Volume")
    if (volume_name == "model-store" or isinstance(min_age_hours, bool)
            or not isinstance(min_age_hours, (int, float)) or not math.isfinite(min_age_hours)
            or not 0 <= min_age_hours <= 24 * 3650):
        raise ValueError("GC requires an Output Volume and a valid retention age")
    now = int(time.time()) if now is None else now
    if type(now) is not int or now <= 0:
        raise ValueError("Invalid GC scan time")
    files = _inventory(volume)
    records, inputs, plans, jobs, receipts = {}, {}, {}, {}, {}
    protected = {}
    # 1. Classify every managed object; unknown execution metadata fails closed.
    for path, meta in sorted(files.items()):
        parts = path.split("/")
        if parts[0] == ".execution":
            if len(parts) == 4 and parts[:3] == [".execution", "v1", "inputs"]:
                name = PurePosixPath(parts[-1])
                ref = {"sha256": name.stem, "extension": name.suffix, "bytes": meta["bytes"]}
                if e.input_key(ref) != path:
                    raise RuntimeError(f"Unknown input object: {path}")
                inputs[path] = ref
                continue
            if len(parts) == 5 and parts[:3] == [".execution", "v1", "workflows"] and parts[-1] == "plan.json":
                plan = records[path] = _record(volume, path)
                w.validate_plan(plan)
                ident = parts[3]
                if plan["volume"] != volume_name or w.plan_key(ident) != path or w.plan_id(plan) != ident:
                    raise RuntimeError(f"Workflow plan identity mismatch: {path}")
                plans[ident] = plan
                continue
            if len(parts) == 9 and parts[:3] == [".execution", "v1", "jobs"]:
                cfg, ident = _job_config(parts, volume_name), parts[7]
                phase = parts[8].removesuffix(".json")
                expected = (e.request_key(cfg, ident) if phase == "request" else
                            e.event_key(cfg, ident, phase) if phase in e.PHASES else None)
                if path != expected:
                    raise RuntimeError(f"Unknown execution object: {path}")
                records[path] = _record(volume, path)
                bucket = jobs.setdefault(e.request_key(cfg, ident), {"config": cfg, "job_id": ident, "events": {}})
                if phase == "request":
                    bucket["request"] = e.validate_request(records[path], cfg, ident)
                else:
                    bucket["events"][phase] = records[path]
                continue
            raise RuntimeError(f"Unknown execution object (GC refuses to guess): {path}")
        if path.startswith(d.READY + "/"):
            record = records[path] = _record(volume, path)
            cfg = _config_from_receipt(record)
            pid = record["prompt_id"]
            if cfg["volume"] != volume_name or path != d.receipt_key(cfg["workflow"], cfg["instance"], pid):
                raise RuntimeError(f"Ready receipt identity mismatch: {path}")
            receipts[path] = d.validate_receipt(record, cfg, pid)
            _protect_receipt(protected, record)

    # 2. Every plan is a root, including future jobs without a request.
    for ident, plan in plans.items():
        for spec in plan["jobs"]:
            for ref in spec["inputs"].values():
                _protect_input(protected, ref)
            _, jid = w.job_identity(ident, spec)
            bucket = jobs.get(e.request_key(spec["delivery"], jid))
            if bucket and "request" in bucket:
                w._check_request_shape(plan, ident, spec, bucket["request"])

    # 3. Validate all job ledgers and mark persisted media and produced receipts.
    active, owners = [], {}
    for key, bucket in sorted(jobs.items()):
        if "request" not in bucket:
            raise RuntimeError(f"Execution event without request: {key}")
        req, events = bucket["request"], bucket["events"]
        for phase, record in events.items():
            expected = e._event(req, phase)
            if any(record.get(k) != v for k, v in expected.items()):
                raise RuntimeError(f"Execution event identity mismatch: {key}/{phase}")
        if events and "claim" not in events:
            raise RuntimeError(f"Execution events without claim: {key}")
        if "failed" in events and "produced" in events:
            raise RuntimeError(f"Conflicting terminal events: {key}")
        if "produced" in events:
            event = events["produced"]
            receipt = d.validate_receipt(event["receipt"], req["config"], event["prompt_id"])
            e._require_scoped_outputs(receipt, req["job_id"])
            if not event["remote_files"]:
                raise RuntimeError(f"Produced event has no selected outputs: {key}")
            for item in event["remote_files"]:
                d.receipt_file(receipt, item)
            ready = receipts.get(d.receipt_key(receipt["workflow"], receipt["instance"], receipt["prompt_id"]))
            if ready is not None and ready != receipt:
                raise RuntimeError(f"Produced/ready receipt conflict: {key}")
            _protect_receipt(protected, receipt)
            state = "produced"
        elif "failed" in events:
            state = "failed"
        elif "claim" in events:
            state = "active_or_unresolved"
            active.append(key)
        else:
            state = "prepared"
        bucket["state"] = state
        owners.setdefault(req["job_id"], []).append((req, state))
        for ref in req["inputs"].values():
            if "source" not in ref:
                _protect_input(protected, ref)

    # 4. Traverse direct output references; never trust arbitrary client paths.
    for key, bucket in sorted(jobs.items()):
        req = bucket["request"]
        for ref in req["inputs"].values():
            if "source" not in ref:
                continue
            src = ref["source"]
            source = jobs.get(e.request_key(src["config"], src["job_id"]))
            if not source or source["state"] != "produced":
                raise RuntimeError(f"Missing completed cloud input source: {key}")
            event = source["events"]["produced"]
            receipt = event["receipt"]
            ready = receipts.get(d.receipt_key(receipt["workflow"], receipt["instance"], receipt["prompt_id"]))
            if (ready != receipt or event["prompt_id"] != src["job_id"]
                    or src["item"] not in event["remote_files"]):
                raise RuntimeError(f"Cloud input source receipt or selection mismatch: {key}")
            file = d.receipt_file(receipt, src["item"])
            if any(ref[k] != file[k] for k in ("sha256", "bytes")):
                raise RuntimeError(f"Cloud input evidence mismatch: {key}")
            _protect(protected, file["key"], file["bytes"])

    # Missing or changed-size live media is a recovery problem, not a GC opportunity.
    # Legacy native ready receipts may legitimately reference paths outside the
    # managed namespace; those are never candidates and need not be inventoried.
    for key, size in protected.items():
        if _managed(key) and (key not in files or files[key]["bytes"] != size):
            raise RuntimeError(f"Missing or changed-size retained media: {key}")

    # 5. Reclaim only old unreferenced CAS inputs and unpublished failed-job media.
    cutoff = now - min_age_hours * 3600
    candidates = []
    for path, meta in sorted(files.items()):
        if path in protected or path in records or meta["mtime"] <= 0 or meta["mtime"] > cutoff:
            continue
        if path in inputs:
            reason = "unreferenced_input"
        else:
            owner = _failed_owner(path, owners)
            if owner is None:
                continue
            reason = "unpublished_failed_job_output"
        candidates.append({**meta, "reason": reason})
    return {"schema": 1, "volume": volume_name, "volume_id": volume.object_id,
            "created_at": now, "min_age_hours": min_age_hours,
            "snapshot": _digest_inventory(files, records),
            "roots": {"plans": len(plans), "requests": len(jobs), "ready_receipts": len(receipts),
                      "produced_receipts": sum(b["state"] == "produced" for b in jobs.values())},
            "protected_media": len(protected), "unresolved_claims": active,
            "candidates": candidates, "candidate_bytes": sum(item["bytes"] for item in candidates)}


def apply(volume, volume_name, plan, *, confirm_quiescent=False, now=None, on_delete=None):
    """Delete exact files only after explicit offline confirmation and a fresh scan.

    The SDK has no transaction across inventory and remove_file. The operator
    must stop ALL clients, apps, mounts and writers for this Volume throughout
    the operation. This is deliberately not an online concurrent collector.
    """
    if not confirm_quiescent:
        raise RuntimeError("GC apply requires an exclusive offline maintenance window")
    now = int(time.time()) if now is None else now
    if (not isinstance(plan, dict) or plan.get("schema") != 1
            or plan.get("volume") != volume_name or type(plan.get("created_at")) is not int
            or not 0 <= now - plan["created_at"] <= PLAN_MAX_AGE_SECONDS):
        raise RuntimeError("GC plan is invalid, expired or for another Volume; rescan")
    fresh = scan(volume, volume_name, min_age_hours=plan["min_age_hours"], now=plan["created_at"])
    if fresh != plan:
        raise RuntimeError("Volume or dependencies changed since GC plan; rescan")
    if fresh["unresolved_claims"]:
        raise RuntimeError("Unresolved execution claims exist; GC apply refused")
    deleted = []
    for item in fresh["candidates"]:
        # Never issue a recursive deletion or delete a metadata/root file.
        volume.remove_file(item["key"], recursive=False)
        deleted.append(item)
        if on_delete is not None:
            on_delete(item)
    return {"volume": volume_name, "deleted": deleted,
            "deleted_bytes": sum(item["bytes"] for item in deleted)}
