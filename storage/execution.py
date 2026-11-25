"""Durable native ComfyUI admission, inputs and immutable execution evidence.

One logical attempt has one write-once claim. A claim is never a lease: absence
of a response or of an in-memory queue cannot authorize another native POST.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from storage.delivery import (
    HEX, MAX_RECEIPT_BYTES, build_receipt, file_digest, normalized_config,
    output_ref, receipt_file, receipt_key, relative_path, token, validate_receipt, verified_copy, volume_receipt,
)
from storage.output_formats import AUDIO, CATEGORIES, VIDEOS

IMAGE_INPUT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MEDIA_INPUT_EXTENSIONS = VIDEOS | AUDIO
INPUT_EXTENSIONS = IMAGE_INPUT_EXTENSIONS | MEDIA_INPUT_EXTENSIONS
# The core loaders resolve relative paths against ComfyUI's native input root.
# Do not assume custom loaders accept the same contract.
_MEDIA_FIELDS = {
    ("LoadImage", "image"): IMAGE_INPUT_EXTENSIONS,
    ("LoadImageMask", "image"): IMAGE_INPUT_EXTENSIONS,
    ("LoadVideo", "file"): VIDEOS,
    ("LoadAudio", "audio"): AUDIO | VIDEOS,
}
PHASES = ("claim", "submitted", "failed", "produced")
PREFIX = ".execution/v1"
OUTPUT_NAMESPACE = "_durable_outputs"


def canonical(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("Execution record too large")
    return data


def sha_json(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def job_id(scope, intent):
    if not isinstance(intent, str) or not HEX.fullmatch(intent):
        raise ValueError("Invalid job intent hash")
    return str(uuid5(NAMESPACE_URL, "comfy-durable-v1:" + sha_json([scope, intent])))


def check_job_id(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("Invalid execution UUID")
    return value


def _base(config, identifier):
    cfg = normalized_config(config)
    if "version" not in cfg:
        raise ValueError("Durable execution requires deployment version")
    return f"{PREFIX}/jobs/{cfg['app']}/{cfg['workflow']}/{cfg['version']}/{cfg['instance']}/{check_job_id(identifier)}"


def request_key(config, identifier):
    return _base(config, identifier) + "/request.json"


def event_key(config, identifier, phase):
    if phase not in PHASES:
        raise ValueError("Unknown execution phase")
    return _base(config, identifier) + "/" + phase + ".json"


def _source_descriptor(source):
    if not isinstance(source, dict) or set(source) != {"config", "job_id", "item"}:
        raise ValueError("Invalid cloud output source")
    cfg = normalized_config(source["config"])
    if cfg != source["config"] or "version" not in cfg:
        raise ValueError("Cloud output source requires exact deployment identity")
    check_job_id(source["job_id"])
    item = source["item"]
    if (not isinstance(item, dict) or set(item) != {"filename", "subfolder", "type"}
            or item["type"] != "output"):
        raise ValueError("Invalid cloud output item")
    output_ref(item)
    return source


def input_name(ref):
    fields = {"sha256", "bytes", "extension"}
    if not isinstance(ref, dict) or set(ref) not in (fields, fields | {"source"}):
        raise ValueError("Invalid persisted input reference")
    digest, size, extension = ref["sha256"], ref["bytes"], ref["extension"]
    if (not isinstance(digest, str) or not HEX.fullmatch(digest) or
            type(size) is not int or size <= 0 or extension not in INPUT_EXTENSIONS):
        raise ValueError("Invalid persisted input evidence")
    if "source" in ref:
        source = _source_descriptor(ref["source"])
        if output_ref(source["item"]).suffix.lower() != extension:
            raise ValueError("Cloud output extension mismatch")
    return "_durable/" + digest + extension


def input_key(ref):
    name = input_name(ref)
    if "source" in ref:
        raise ValueError("Cloud output inputs require authoritative receipt resolution")
    return PREFIX + "/inputs/" + name.split("/")[-1]


def _check_source_scope(source, config, identifier):
    _source_descriptor(source)
    if source["config"]["volume"] != config["volume"]:
        raise ValueError("Cloud output handoff requires the same Volume")
    if source["config"] == config and source["job_id"] == identifier:
        raise ValueError("Cloud output cannot reference its own attempt")


def _source_file(volume, config, identifier, source):
    """Read only immutable metadata; never stream output bytes on the staging client."""
    _check_source_scope(source, config, identifier)
    upstream = load_request(volume, source["config"], source["job_id"])
    produced = inspect_execution(volume, upstream)
    if produced["status"] != "produced" or produced["prompt_id"] != source["job_id"]:
        raise RuntimeError("Cloud output source has no completed execution")
    receipt = produced["receipt"]
    ready = volume_receipt(volume, source["config"], produced["prompt_id"])
    if ready is None or ready != receipt:
        raise RuntimeError("Cloud output source has no matching ready receipt")
    if source["item"] not in produced["remote_files"]:
        raise ValueError("Cloud output is not a selected upstream artifact")
    _require_scoped_outputs(receipt, source["job_id"])
    return receipt_file(receipt, source["item"])


def cloud_input_ref(volume, config, identifier, source):
    file = _source_file(volume, config, identifier, source)
    ref = {"sha256": file["sha256"], "bytes": file["bytes"],
           "extension": output_ref(source["item"]).suffix.lower(), "source": source}
    input_name(ref)
    return ref


def _read_json(volume, key):
    data = bytearray()
    try:
        for chunk in volume.read_file(key):
            data.extend(chunk)
            if len(data) > MAX_RECEIPT_BYTES:
                raise ValueError("Execution record too large")
    except FileNotFoundError:
        return None
    return json.loads(data)


def _verify_chunks(chunks, ref):
    size, digest = 0, hashlib.sha256()
    for chunk in chunks:
        size += len(chunk)
        if size > ref["bytes"]:
            raise RuntimeError("Persisted input/output exceeds expected size")
        digest.update(chunk)
    if size != ref["bytes"] or digest.hexdigest() != ref["sha256"]:
        raise RuntimeError("Persisted input/output SHA-256/size mismatch")


def is_write_conflict(exc, key):
    """Modal SDK 1.6.4 V2 may report an existing file as InvalidError.

    Match only the observed no-overwrite conflict for this exact key. All
    other errors remain failures; callers read back and validate the winner.
    """
    if isinstance(exc, FileExistsError):
        return True
    from modal.exception import InvalidError
    return isinstance(exc, InvalidError) and str(exc) in {
        f'path "/{key.lstrip("/")}" already exists',
        f'path "{key.lstrip("/")}" already exists',
    }


def put_once(volume, key, value):
    """Create, never replace, an immutable JSON record. Return True to winner."""
    import io
    raw = canonical(value)
    try:
        with volume.batch_upload(force=False) as batch:
            batch.put_file(io.BytesIO(raw), key)
    except Exception as exc:
        if not is_write_conflict(exc, key):
            raise
        if _read_json(volume, key) != value:
            raise RuntimeError(f"Conflicting immutable record: {key}")
        return False
    return True


def stage_input(volume, path, expected_sha256=None):
    """Snapshot local media before upload; no partial or mutable input identity."""
    path = Path(path)
    extension = path.suffix.lower()
    if extension not in INPUT_EXTENSIONS or not path.is_file():
        raise ValueError(f"Durable input must be an existing supported media file: {path}")
    before = path.stat()
    digest, size = hashlib.sha256(), 0
    with tempfile.TemporaryDirectory(prefix="comfy-input-") as folder:
        copy = Path(folder) / ("input" + extension)
        with path.open("rb") as source, copy.open("xb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                target.write(chunk)
        after = path.stat()
        if size != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size, after.st_mtime_ns, after.st_ino):
            raise RuntimeError("Input changed while staging")
        ref = {"sha256": digest.hexdigest(), "bytes": size, "extension": extension}
        if expected_sha256 is not None and ref["sha256"] != expected_sha256:
            raise RuntimeError("Input changed after intent hashing or upstream delivery")
        key = input_key(ref)
        try:
            _verify_chunks(volume.read_file(key), ref)
        except FileNotFoundError:
            try:
                with volume.batch_upload(force=False) as batch:
                    batch.put_file(copy, key)
            except Exception as exc:
                if not is_write_conflict(exc, key):
                    raise
                _verify_chunks(volume.read_file(key), ref)
        return ref


def validate_input_field(graph, field, extension):
    """Require a verified native path-loading field for video/audio inputs."""
    node_id, _, name = field.partition(".")
    node = graph.get(node_id, {})
    allowed = _MEDIA_FIELDS.get((node.get("class_type"), name))
    if extension not in INPUT_EXTENSIONS or (
        extension not in (allowed if allowed is not None else IMAGE_INPUT_EXTENSIONS)
    ):
        raise ValueError(f"Unsupported durable input {field} ({node.get('class_type')}): {extension}")


def validate_request(request, config, identifier):
    cfg = normalized_config(config)
    if not isinstance(request, dict) or set(request) != {
        "schema", "config", "job_id", "intent", "graph", "inputs", "output_nodes"
    } or request["schema"] != 1:
        raise ValueError("Unsupported durable request")
    if request["config"] != cfg or request["job_id"] != check_job_id(identifier):
        raise ValueError("Durable request identity mismatch")
    if not isinstance(request["intent"], str) or not HEX.fullmatch(request["intent"]):
        raise ValueError("Invalid request intent")
    graph, inputs, nodes = request["graph"], request["inputs"], request["output_nodes"]
    if not isinstance(graph, dict) or not graph or not isinstance(inputs, dict):
        raise ValueError("Invalid request graph or inputs")
    for field, ref in inputs.items():
        node, sep, name = field.partition(".")
        if not sep or graph.get(node, {}).get("inputs", {}).get(name) != input_name(ref):
            raise ValueError(f"Input reference does not match graph: {field}")
        validate_input_field(graph, field, ref["extension"])
        if "source" in ref:
            _check_source_scope(ref["source"], cfg, identifier)
    if nodes is not None and (not isinstance(nodes, list) or not nodes or
                              any(not isinstance(n, str) or n not in graph for n in nodes)):
        raise ValueError("Invalid output node selection")
    canonical(request)
    return request


def load_request(volume, config, identifier):
    request = _read_json(volume, request_key(config, identifier))
    if request is None:
        raise FileNotFoundError(f"Missing durable request: {identifier}")
    return validate_request(request, config, identifier)


def scoped_output_graph(graph, identifier, output_nodes=None):
    """Derive a job-private native output graph without changing the request.

    Keep category-first prefixes (e.g. video/...) for existing asset browsers.
    A new attempt gets a new namespace even when the original graph is reused.
    """
    identifier = check_job_id(identifier)
    prompt = json.loads(json.dumps(graph, ensure_ascii=False))
    scoped = set()
    for node_id, node in prompt.items():
        inputs = node.get("inputs", {})
        if "filename_prefix" not in inputs:
            if str(node.get("class_type", "")).startswith("Save"):
                raise ValueError(f"Durable save node requires a literal filename_prefix: {node_id}")
            continue
        prefix = inputs["filename_prefix"]
        if not isinstance(prefix, str):
            raise ValueError(f"Durable filename_prefix must be a literal string: {node_id}")
        parts = list(relative_path(prefix).parts)
        if OUTPUT_NAMESPACE in parts:
            raise ValueError("Reserved durable output namespace in filename_prefix")
        category = parts.pop(0) if len(parts) > 1 and parts[0] in CATEGORIES else None
        inputs["filename_prefix"] = "/".join(
            ([category] if category else []) + [OUTPUT_NAMESPACE, identifier] + parts)
        scoped.add(node_id)
    if not scoped or (output_nodes is not None and not set(output_nodes).issubset(scoped)):
        raise ValueError("Durable outputs require explicit, literal filename_prefix fields")
    return prompt


def _require_scoped_outputs(receipt, identifier):
    """Fail closed if a native output bypasses its assigned namespace."""
    for file in receipt["files"]:
        parts = list(output_ref(file).parts)
        if parts[0] in CATEGORIES:
            parts.pop(0)
        if len(parts) < 3 or parts[:2] != [OUTPUT_NAMESPACE, identifier]:
            raise RuntimeError(f"Native output escaped durable job namespace: {file['key']}")


def stage_request(volume, config, identifier, intent, graph, settings, output_nodes=None,
                  expected_inputs=None, output_inputs=None):
    """Persist a resolved graph with local objects or verified cloud output references."""
    prompt = json.loads(json.dumps(graph, ensure_ascii=False))
    config = normalized_config(config)
    output_inputs = output_inputs or {}
    if set(output_inputs) - set(settings):
        raise ValueError("Cloud output inputs must match settings")
    refs, cache = {}, {}
    for field, value in settings.items():
        node, sep, name = field.partition(".")
        if not sep or name not in prompt.get(node, {}).get("inputs", {}):
            raise ValueError(f"Unknown graph input: {field}")
        if field in output_inputs:
            if value is not None:
                raise ValueError("Cloud output input cannot also have a local value")
            ref = cloud_input_ref(volume, config, identifier, output_inputs[field])
            validate_input_field(prompt, field, ref["extension"])
            refs[field] = ref
            value = input_name(ref)
        elif isinstance(value, str) and value.startswith("@"):
            path = str(Path(value[1:]).resolve())
            validate_input_field(prompt, field, Path(path).suffix.lower())
            expected = (expected_inputs or {}).get(field)
            if path not in cache:
                cache[path] = stage_input(volume, path, expected_sha256=expected)
            refs[field] = cache[path]
            if expected is not None and refs[field]["sha256"] != expected:
                raise RuntimeError(f"Input changed after intent hashing or upstream delivery: {field}")
            value = input_name(refs[field])
        prompt[node]["inputs"][name] = value
    request = {"schema": 1, "config": normalized_config(config), "job_id": identifier,
               "intent": intent, "graph": prompt, "inputs": refs, "output_nodes": output_nodes}
    validate_request(request, config, identifier)
    # Validate the derived native graph before publishing an executable request.
    scoped_output_graph(prompt, identifier, output_nodes)
    put_once(volume, request_key(config, identifier), request)
    return request


def materialize_inputs(volume, request, input_root):
    """Reconstruct exact input bytes in this container before admission."""
    root = Path(input_root)
    directory = root / "_durable"
    if root.is_symlink() or directory.is_symlink():
        raise ValueError("Unsafe input directory")
    directory.mkdir(parents=True, exist_ok=True)
    # Resolve and validate every source even when two inputs have identical bytes.
    resolved = {}
    for ref in request["inputs"].values():
        expected = {k: ref[k] for k in ("bytes", "sha256")}
        if "source" in ref:
            file = _source_file(volume, request["config"], request["job_id"], ref["source"])
            if expected != {k: file[k] for k in expected}:
                raise RuntimeError("Cloud output evidence changed")
            key = file["key"]
        else:
            key = input_key(ref)
        resolved.setdefault(input_name(ref), (key, expected))
    for name, (key, expected) in resolved.items():
        target = root / name
        if target.is_symlink():
            raise ValueError("Unsafe input target")
        if target.is_file() and file_digest(target) == expected:
            continue
        verified_copy(volume.read_file(key), target, expected)


def _event(request, phase, **fields):
    return {"schema": 1, "phase": phase, "job_id": request["job_id"],
            "request_sha256": sha_json(request), **fields}


def _load_event(volume, request, phase):
    record = _read_json(volume, event_key(request["config"], request["job_id"], phase))
    if record is None:
        return None
    expected = _event(request, phase)
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise ValueError("Execution event identity mismatch")
    return record


def _write_event(volume, request, phase, **fields):
    record = _event(request, phase, **fields)
    put_once(volume, event_key(request["config"], request["job_id"], phase), record)
    return record


def inspect_execution(volume, request):
    """Derived status from immutable records, not from a mutable local queue."""
    produced = _load_event(volume, request, "produced")
    if produced is not None:
        validate_receipt(produced["receipt"], request["config"], produced["prompt_id"])
        return {"status": "produced", **produced}
    failed = _load_event(volume, request, "failed")
    if failed is not None:
        return {"status": "failed", **failed}
    submitted = _load_event(volume, request, "submitted")
    if submitted is not None:
        return {"status": "submitted", **submitted}
    claim = _load_event(volume, request, "claim")
    if claim is not None:
        return {"status": "admitted", **claim}
    return {"status": "prepared", "job_id": request["job_id"]}


def finalize_produced(volume, request, produced, verify=True):
    """Recover a committed success even if GPU died before publishing ready."""
    receipt = validate_receipt(produced["receipt"], request["config"], produced["prompt_id"])
    ready = volume_receipt(volume, request["config"], produced["prompt_id"])
    if ready is None:
        if verify:
            for file in receipt["files"]:
                _verify_chunks(volume.read_file(file["key"]), file)
        put_once(volume, receipt_key(receipt["workflow"], receipt["instance"], receipt["prompt_id"]), receipt)
        ready = volume_receipt(volume, request["config"], produced["prompt_id"])
    if ready != receipt:
        raise RuntimeError("Execution receipt missing or inconsistent")
    files = produced["remote_files"]
    for item in files:
        from storage.delivery import receipt_file
        receipt_file(receipt, item)
    if not files:
        raise ValueError("Missing selected output references")
    return {**produced, "status": "completed", "receipt": ready}


class RejectedPrompt(RuntimeError):
    pass


class NativeEngine:
    """Localhost native API only. Never retry the admission POST."""
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def _json(self, path, body=None):
        data = canonical(body) if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)

    def submit(self, graph, identifier):
        try:
            reply = self._json("/prompt", {"prompt": graph, "prompt_id": identifier,
                                           "client_id": identifier})
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                raise RejectedPrompt(exc.read(1000).decode("utf-8", "replace")) from exc
            raise
        return token(reply["prompt_id"], "native prompt ID")

    def history(self, identifier):
        return self._json("/history/" + token(identifier, "native prompt ID")).get(identifier)

    def contains(self, identifier):
        queue = self._json("/queue")
        return any(len(item) > 1 and item[1] == identifier
                   for kind in ("queue_running", "queue_pending") for item in queue.get(kind, []))


def _selected_files(history, output_nodes, receipt):
    from storage.delivery import receipt_file
    files, seen = [], set()
    for node, output in history.get("outputs", {}).items():
        if output_nodes is not None and node not in output_nodes:
            continue
        for items in output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or "filename" not in item or item.get("type", "output") != "output":
                    continue
                ref = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": "output"}
                key = receipt_file(receipt, ref)["key"]
                if key not in seen:
                    files.append(ref)
                    seen.add(key)
    if not files:
        raise RuntimeError("No selected persisted output")
    return files


def execute(volume, request, engine, epoch, input_root, output_root, commit_lock,
            dual=False, timeout=13800, poll_interval=1, missing_grace=15):
    """Own native execution through durable publication; safe on repeated RPCs.

    The no-overwrite claim is the admission barrier, including across container
    replacement. A new epoch never guesses that an old claimed job is safe to
    replay. Only immutable produced evidence allows a CPU-side completion.
    """
    cfg, identifier = request["config"], request["job_id"]
    validate_request(request, cfg, identifier)
    state = inspect_execution(volume, request)
    if state["status"] == "produced":
        return finalize_produced(volume, request, state)
    if state["status"] == "failed":
        return state

    if state["status"] == "prepared":
        # Derive the private output namespace before admission, never on retry by mutation.
        native_graph = scoped_output_graph(request["graph"], identifier, request["output_nodes"])
        materialize_inputs(volume, request, input_root)
        claim = _event(request, "claim", epoch=epoch, owner=uuid4().hex, prompt_id=identifier)
        try:
            with __import__("io").BytesIO(canonical(claim)) as data:
                with volume.batch_upload(force=False) as batch:
                    batch.put_file(data, event_key(cfg, identifier, "claim"))
            won = True
        except Exception as exc:
            if not is_write_conflict(exc, event_key(cfg, identifier, "claim")):
                raise
            won = False
        if won:
            try:
                actual = engine.submit(native_graph, identifier)
            except RejectedPrompt as exc:
                return {"status": "failed", **_write_event(volume, request, "failed",
                         epoch=epoch, prompt_id=identifier, error=str(exc)[:1500])}
            except (OSError, ValueError, KeyError):
                # A lost response is not permission to POST again. Current
                # ComfyUI supports client UUIDs; reconcile via history/queue.
                state = {"status": "admitted", **claim}
            else:
                state = {"status": "submitted", **_write_event(volume, request, "submitted",
                         epoch=epoch, prompt_id=actual)}
        else:
            state = inspect_execution(volume, request)
            if state["status"] == "produced":
                return finalize_produced(volume, request, state)
            if state["status"] == "failed":
                return state
            if state["status"] == "prepared":
                raise RuntimeError("Admission claim not visible; no native POST sent")

    if state.get("epoch") != epoch:
        # Do not write a competing terminal event: the old call could still
        # publish a valid success during platform replacement.
        return {**state, "status": "interrupted",
                "error": "Prior execution belongs to a different container epoch; no automatic replay"}
    prompt_id = state["prompt_id"]
    deadline, absent_since = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            history = engine.history(prompt_id)
            present = history is not None or engine.contains(prompt_id)
        except (OSError, ValueError, KeyError):
            # HTTP/transport failures are not evidence that a job disappeared.
            time.sleep(poll_interval)
            continue
        if history is not None:
            status = history.get("status", {})
            if status.get("status_str") == "error":
                return {"status": "failed", **_write_event(volume, request, "failed",
                        epoch=epoch, prompt_id=prompt_id, error=json.dumps(status, ensure_ascii=False)[:1500])}
            if status.get("status_str") == "success" and status.get("completed") is True:
                receipt = build_receipt(Path(output_root), cfg, prompt_id, history,
                                        dual=dual, version=cfg["version"])
                _require_scoped_outputs(receipt, identifier)
                files = _selected_files(history, request["output_nodes"], receipt)
                # First publish the actual bytes, then the evidence of success.
                # A CPU reader can reconstruct ready after a crash in this gap.
                with commit_lock:
                    volume.commit()
                    produced = _write_event(volume, request, "produced", epoch=epoch,
                                            prompt_id=prompt_id, receipt=receipt, remote_files=files)
                    return finalize_produced(volume, request, produced, verify=False)
        if present:
            absent_since = None
        elif absent_since is None:
            absent_since = time.monotonic()
        elif time.monotonic() - absent_since >= missing_grace:
            return {**state, "status": "uncertain",
                    "error": "Native queue/history missing; refusing duplicate submission"}
        time.sleep(poll_interval)
    return {**state, "status": "uncertain", "error": "Execution observation deadline exceeded"}
