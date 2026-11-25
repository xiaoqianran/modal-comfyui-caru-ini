"""Immutable workflow plans stored alongside durable execution evidence.

A published plan contains every initial media reference and the expanded DAG.
It is a recipe, not an execution claim; each job keeps the v1 claim protocol.
"""
from __future__ import annotations

import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from storage import execution as e
from storage.delivery import normalized_config, receipt_file, token
from storage.output_formats import AUDIO, VIDEOS

KINDS = {"image": e.IMAGE_INPUT_EXTENSIONS, "video": VIDEOS,
         "audio": AUDIO, "file": e.INPUT_EXTENSIONS}
_PLAN_FIELDS = {"schema", "name", "volume", "mode", "graphs", "jobs"}
_JOB_FIELDS = {"step", "job", "endpoint", "base", "delivery", "graph",
               "settings", "inputs", "sources", "output_nodes", "collect"}


def _label(value):
    token(value, "workflow label")
    # Every job is also a portable local output directory.
    stem = value.split(".")[0].upper()
    if value.endswith(".") or stem in {"CON", "PRN", "AUX", "NUL", *(
            f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(10))}:
        raise ValueError(f"Unsafe portable workflow label: {value}")
    return value


def plan_id(plan):
    return e.job_id(["workflow-plan-v1", plan["volume"]], e.sha_json(plan))


def plan_key(identifier):
    return f"{e.PREFIX}/workflows/{e.check_job_id(identifier)}/plan.json"


def job_key(spec):
    return spec["step"], spec["job"]


def job_identity(identifier, spec):
    intent = e.sha_json(spec)
    return intent, e.job_id(["workflow-plan-v1", identifier, *job_key(spec)], intent)


def _graph_input(graph, field):
    node, sep, name = field.partition(".")
    if not sep or name not in graph.get(node, {}).get("inputs", {}):
        raise ValueError(f"Unknown workflow graph input: {field}")
    return graph[node]["inputs"], name


def _base_graph(plan, spec):
    graph = json.loads(json.dumps(plan["graphs"][spec["graph"]], ensure_ascii=False))
    for field, value in spec["settings"].items():
        inputs, name = _graph_input(graph, field)
        inputs[name] = value
    for field, ref in spec["inputs"].items():
        inputs, name = _graph_input(graph, field)
        inputs[name] = e.input_name(ref)
    return graph


def validate_plan(plan):
    """Validate the complete plan before publication or use, without media reads."""
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS or type(plan["schema"]) is not int or plan["schema"] != 1:
        raise ValueError("Unsupported workflow plan")
    _label(plan["name"])
    token(plan["volume"], "workflow Volume")
    if plan["mode"] not in ("stream", "batch"):
        raise ValueError("Invalid workflow mode")
    graphs, jobs = plan["graphs"], plan["jobs"]
    if not isinstance(graphs, dict) or not graphs or not isinstance(jobs, list) or not jobs:
        raise ValueError("Workflow plan requires graphs and jobs")
    for digest, graph in graphs.items():
        if not isinstance(graph, dict) or not graph or digest != e.sha_json(graph):
            raise ValueError("Workflow graph digest mismatch")
        for node_id, node in graph.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                raise ValueError(f"Invalid workflow graph node: {node_id}")
    seen, folded, used_graphs, step_names = set(), set(), set(), {}
    for spec in jobs:
        if not isinstance(spec, dict) or set(spec) != _JOB_FIELDS:
            raise ValueError("Invalid workflow job")
        step, job = _label(spec["step"]), _label(spec["job"])
        key = (step, job)
        lower = tuple(part.casefold() for part in key)
        if lower in folded or step_names.get(lower[0], step) != step:
            raise ValueError("Duplicate or case-colliding workflow job")
        step_names[lower[0]] = step
        _label(spec["endpoint"])
        base = spec["base"]
        if not isinstance(base, str):
            raise ValueError("Invalid workflow endpoint URL")
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.netloc or url.username is not None
                or url.password is not None or url.query or url.fragment or base.rstrip("/") != base):
            raise ValueError("Workflow endpoint must be a plain deployed URL")
        cfg = normalized_config(spec["delivery"])
        if cfg != spec["delivery"] or cfg.get("volume") != plan["volume"] or "version" not in cfg:
            raise ValueError("Workflow jobs require exact deployment identity and one Volume")
        if spec["collect"] not in ("local", "cloud") or spec["graph"] not in graphs:
            raise ValueError("Invalid workflow collection or graph reference")
        used_graphs.add(spec["graph"])
        settings, inputs, sources = (spec[k] for k in ("settings", "inputs", "sources"))
        if not all(isinstance(x, dict) for x in (settings, inputs, sources)):
            raise ValueError("Invalid workflow inputs")
        if set(settings) & set(inputs) or set(settings) & set(sources) or set(inputs) & set(sources):
            raise ValueError("Workflow input fields overlap")
        graph = _base_graph(plan, spec)
        for field, ref in inputs.items():
            if not isinstance(ref, dict) or set(ref) != {"sha256", "bytes", "extension"}:
                raise ValueError("Workflow initial input must be a persisted content object")
            if field.partition(".")[2] == "filename_prefix":
                raise ValueError("Workflow input cannot set an output filename prefix")
            e.validate_input_field(graph, field, ref["extension"])
        for field, source in sources.items():
            if (not isinstance(source, dict) or set(source) != {"job", "kind"}
                    or not isinstance(source["job"], list) or len(source["job"]) != 2
                    or not all(isinstance(part, str) for part in source["job"])
                    or tuple(source["job"]) not in seen or source["kind"] not in KINDS):
                raise ValueError("Workflow dependency must select an earlier job's media")
            target, name = _graph_input(graph, field)
            if name == "filename_prefix":
                raise ValueError("Workflow source cannot set an output filename prefix")
            node = graph[field.partition(".")[0]]
            allowed = e._MEDIA_FIELDS.get((node.get("class_type"), name), e.IMAGE_INPUT_EXTENSIONS)
            if not (KINDS[source["kind"]] & allowed):
                raise ValueError(f"Incompatible workflow media dependency: {field}")
            target[name] = "_durable/placeholder"
        # Native media loaders must have tracked inputs, even on later stages.
        # Arbitrary custom nodes may still depend on external services; those
        # are outside the explicit-input contract.
        for node_id, node in graph.items():
            for cls, field in e._MEDIA_FIELDS:
                if node.get("class_type") == cls and f"{node_id}.{field}" not in inputs and f"{node_id}.{field}" not in sources:
                    raise ValueError(f"Untracked native media input: {node_id}.{field}; use @file or $step.kind")
        for node_id, node in graph.items():
            for field, value in node["inputs"].items():
                if (isinstance(value, list) and len(value) == 2
                        and isinstance(value[0], str) and isinstance(value[1], int)
                        and value[0] not in graph):
                    raise ValueError(f"Broken workflow graph link: {node_id}.{field}")
        nodes = spec["output_nodes"]
        if nodes is not None and (not isinstance(nodes, list) or not nodes
                or any(not isinstance(n, str) or n not in graph for n in nodes)):
            raise ValueError("Invalid workflow output nodes")
        e.scoped_output_graph(graph, "00000000-0000-0000-0000-000000000000", nodes)
        seen.add(key)
        folded.add(lower)
    if used_graphs != set(graphs):
        raise ValueError("Unused workflow graph")
    e.canonical(plan)
    return plan


def publish_plan(volume, plan):
    validate_plan(plan)
    identifier = plan_id(plan)
    e.put_once(volume, plan_key(identifier), plan)
    return identifier


def load_plan(volume, volume_name, identifier):
    plan = e._read_json(volume, plan_key(identifier))
    if plan is None:
        raise FileNotFoundError(f"Missing workflow plan: {identifier}")
    validate_plan(plan)
    if plan["volume"] != volume_name or plan_id(plan) != identifier:
        raise ValueError("Workflow plan identity mismatch")
    return plan


def build_request(volume, plan, identifier, spec, selected_outputs):
    """Bind immutable input refs and selected upstream outputs without local I/O.

    selected_outputs maps earlier (step, job) keys to authoritative output items.
    The source receipt and job-private namespace are checked by cloud_input_ref.
    """
    intent, ident = job_identity(identifier, spec)
    cfg = spec["delivery"]
    graph = _base_graph(plan, spec)
    refs = dict(spec["inputs"])
    by_key = {job_key(s): s for s in plan["jobs"]}
    source_refs = {}
    for field, source in spec["sources"].items():
        key = tuple(source["job"])
        upstream = by_key[key]
        _, upstream_id = job_identity(identifier, upstream)
        extensions = KINDS[source["kind"]]
        matches = [item for item in selected_outputs[key]
                   if PurePosixPath(item["filename"]).suffix.lower() in extensions]
        if len(matches) != 1:
            raise ValueError(f"Workflow reference {key}.{source['kind']}: expected one media output, found {len(matches)}")
        descriptor = {"config": upstream["delivery"], "job_id": upstream_id, "item": matches[0]}
        source_key = e.sha_json(descriptor)
        if source_key not in source_refs:
            source_refs[source_key] = e.cloud_input_ref(volume, cfg, ident, descriptor)
        ref = source_refs[source_key]
        target, name = _graph_input(graph, field)
        target[name] = e.input_name(ref)
        refs[field] = ref
    request = {"schema": 1, "config": cfg, "job_id": ident, "intent": intent,
               "graph": graph, "inputs": refs, "output_nodes": spec["output_nodes"]}
    e.validate_request(request, cfg, ident)
    e.scoped_output_graph(graph, ident, spec["output_nodes"])
    return request


def _check_request_shape(plan, identifier, spec, request):
    """Check existing graph/input binding against the plan without reading media."""
    intent, ident = job_identity(identifier, spec)
    e.validate_request(request, spec["delivery"], ident)
    graph = _base_graph(plan, spec)
    if (request["intent"] != intent or request["output_nodes"] != spec["output_nodes"]
            or set(request["inputs"]) != (set(spec["inputs"]) | set(spec["sources"]))):
        raise RuntimeError(f"Stored request differs from workflow identity: {ident}")
    for field, ref in spec["inputs"].items():
        if request["inputs"][field] != ref:
            raise RuntimeError(f"Stored initial input differs from workflow plan: {ident}")
    by_key = {job_key(s): s for s in plan["jobs"]}
    for field, source in spec["sources"].items():
        ref = request["inputs"][field]
        upstream = by_key[tuple(source["job"])]
        _, upstream_id = job_identity(identifier, upstream)
        descriptor = ref.get("source")
        if (descriptor is None or descriptor["config"] != upstream["delivery"]
                or descriptor["job_id"] != upstream_id
                or ref["extension"] not in KINDS[source["kind"]]):
            raise RuntimeError(f"Stored source differs from workflow dependency: {ident}")
        target, name = _graph_input(graph, field)
        target[name] = e.input_name(ref)
    if request["graph"] != graph:
        raise RuntimeError(f"Stored graph differs from workflow plan: {ident}")


def persist_request(volume, request):
    """An existing request must equal the plan-derived request, not just its ID."""
    key = e.request_key(request["config"], request["job_id"])
    existing = e._read_json(volume, key)
    if existing is None:
        e.put_once(volume, key, request)
    elif existing != request:
        raise RuntimeError(f"Stored request differs from workflow plan: {request['job_id']}")
    return request


def inspect_plan(volume, plan, identifier):
    """Read ledger state only; never dispatch or repair a missing ready marker."""
    jobs = []
    for spec in plan["jobs"]:
        _, ident = job_identity(identifier, spec)
        cfg = spec["delivery"]
        request = e._read_json(volume, e.request_key(cfg, ident))
        state = {"status": "not_prepared"}
        if request is not None:
            _check_request_shape(plan, identifier, spec, request)
            state = e.inspect_execution(volume, request)
            if state["status"] == "produced":
                ready = e.volume_receipt(volume, cfg, state["prompt_id"])
                if ready is not None:
                    if ready != state["receipt"] or not state["remote_files"]:
                        raise RuntimeError("Workflow ready receipt inconsistent with produced evidence")
                    for item in state["remote_files"]:
                        receipt_file(ready, item)
                    state = {**state, "status": "completed"}
        item = {"step": spec["step"], "job": spec["job"], "job_id": ident,
                "status": state["status"], "collect": spec["collect"],
                "depends_on": sorted({tuple(s["job"]) for s in spec["sources"].values()})}
        if state.get("prompt_id"):
            item["prompt_id"] = state["prompt_id"]
        if state.get("error"):
            item["error"] = state["error"]
        jobs.append(item)
    return {"workflow_id": identifier, "volume": plan["volume"],
            "name": plan["name"], "mode": plan["mode"], "jobs": jobs}
