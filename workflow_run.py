"""Prepare, inspect and resume complete durable workflows by cloud UUID."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import batch_run as runner
from storage import execution as e, workflow as w, execution_client as ec
from storage.delivery import DEFAULT_VOLUME


def _volume(name):
    import modal
    return modal.Volume.from_name(name, version=2)


def _resolve_file(value, task_dir, root):
    path = Path(value[1:])
    if not path.is_absolute():
        candidate = task_dir / path
        path = candidate if candidate.is_file() else root / path
    return path.resolve()


def _source(raw, jobs):
    parts = raw[1:].split(".")
    if len(parts) == 2:
        candidates = [key for key in jobs if key[0] == parts[0]]
        if len(candidates) != 1:
            raise ValueError(f"Ambiguous workflow dependency: {raw}")
        key, kind = candidates[0], parts[1]
    elif len(parts) == 3:
        key, kind = tuple(parts[:2]), parts[2]
    else:
        raise ValueError(f"Invalid workflow dependency: {raw}")
    return {"job": list(key), "kind": kind}


def compile_plan(task_path, endpoints_path):
    """Resolve source paths only during preparation; never persist local paths."""
    task_path, endpoints_path = Path(task_path).resolve(), Path(endpoints_path).resolve()
    task = json.loads(task_path.read_text(encoding="utf-8-sig"))
    endpoints = json.loads(endpoints_path.read_text(encoding="utf-8-sig"))
    name = runner._safe_name(task["name"])
    root = endpoints_path.parent
    jobs, _ = runner._build_jobs(task["steps"], endpoints, root, root / "outputs" / name,
                                 task_path.parent, run_name=name)
    if not jobs or any(job["execution"] != "durable" for job in jobs.values()):
        raise ValueError("Workflow plans require every job to use durable execution")
    volumes = {job["delivery"]["volume"] for job in jobs.values()}
    if len(volumes) != 1:
        raise ValueError("Workflow plans require a single Output Volume")
    plan = {"schema": 1, "name": name, "volume": volumes.pop(),
            "mode": task.get("mode", "stream"), "graphs": {}, "jobs": []}
    files = {}
    for (sid, jid), job in jobs.items():
        # Overridden template defaults are not part of execution. Strip them
        # so an old local filename in a loader placeholder is not published.
        graph = json.loads(json.dumps(job["graph"], ensure_ascii=False))
        for field in job["settings"]:
            node, _, name = field.partition(".")
            graph[node]["inputs"][name] = None
        digest = e.sha_json(graph)
        plan["graphs"][digest] = graph
        spec = {"step": sid, "job": jid, "endpoint": job["endpoint"], "base": job["base"],
                "delivery": job["delivery"], "graph": digest, "settings": {}, "inputs": {},
                "sources": {}, "output_nodes": job["output_nodes"], "collect": job["collect"]}
        for field, raw in job["settings"].items():
            if isinstance(raw, str) and raw.startswith("$") and not raw.startswith("$item."):
                spec["sources"][field] = _source(raw, jobs)
                continue
            value = runner._input_value(raw, job["entry"], {})
            if isinstance(value, str) and value.startswith("@"):
                path = _resolve_file(value, task_path.parent, root)
                ref = {"sha256": job["local_input_hashes"][field], "bytes": path.stat().st_size,
                       "extension": path.suffix.lower()}
                e.input_name(ref)
                spec["inputs"][field] = ref
                identity = (str(path), ref["sha256"], ref["extension"])
                files[identity] = (path, ref)
            else:
                spec["settings"][field] = value
        plan["jobs"].append(spec)
    w.validate_plan(plan)
    return plan, list(files.values())


def prepare(task_path, endpoints_path="endpoints.json", *, volume=None):
    """Publish only after ALL initial inputs (even future jobs) are persisted."""
    plan, files = compile_plan(task_path, endpoints_path)
    # Verify all exact deployment identities and required capabilities on CPU
    # before uploading any media or publishing a plan.
    probes = {}
    for spec in plan["jobs"]:
        identity = (spec["base"], e.sha_json(spec["delivery"]))
        if identity not in probes:
            probes[identity] = ec.DurableClient(spec["delivery"], spec["base"]).preflight()
        info = probes[identity]
        if spec["sources"] and info.get("execution_cloud_inputs") != 1:
            raise RuntimeError("Deployment does not support direct cloud outputs; redeploy first")
        if (any(ref["extension"] in e.MEDIA_INPUT_EXTENSIONS for ref in spec["inputs"].values())
                and info.get("execution_media_inputs") != 1):
            raise RuntimeError("Deployment does not support durable video/audio inputs; redeploy first")
    volume = volume if volume is not None else _volume(plan["volume"])
    for path, expected in files:
        actual = e.stage_input(volume, path, expected_sha256=expected["sha256"])
        if actual != expected:
            raise RuntimeError("Workflow input changed before plan publication")
    identifier = w.publish_plan(volume, plan)
    return {"workflow_id": identifier, "volume": plan["volume"], "jobs": len(plan["jobs"])}


def _build_run_jobs(plan, identifier, out):
    jobs = {}
    for spec in plan["jobs"]:
        key = w.job_key(spec)
        intent, ident = w.job_identity(identifier, spec)
        folder = out.joinpath(*key)
        graph = plan["graphs"][spec["graph"]]
        runner._validate_graph(graph, {field: None for field in (
            spec["settings"].keys() | spec["inputs"].keys() | spec["sources"].keys())},
            spec["output_nodes"])
        jobs[key] = {"base": spec["base"], "endpoint": spec["endpoint"],
                     "delivery": spec["delivery"], "execution": "durable", "collect": spec["collect"],
                     "graph": graph, "output_nodes": spec["output_nodes"],
                     "deps": {tuple(src["job"]) for src in spec["sources"].values()},
                     "state": "pending", "folder": folder, "status_file": folder / "status.json",
                     "durable_job_id": ident, "intent_hash": intent, "spec": spec,
                     "workflow_plan": identifier}
    return jobs


def _submit_planned(key, job, completed, plan, identifier, volume):
    # Cloud ledger is authoritative. Rebuild and compare the full request even
    # for an existing job; no mutable local state or file is needed for this.
    selected = {dep: completed[dep[0]][dep[1]].files for dep in job["deps"]}
    request = w.build_request(volume, plan, identifier, job["spec"], selected)
    w.persist_request(volume, request)
    runner._save_json(job["folder"] / "workflow.json", request["graph"])
    client = ec.DurableClient(job["delivery"], job["base"])
    runner._dispatch_durable(key, job, client)


def _collect_planned(key, job, files):
    # Honor local/cloud collection policy, but ALL dependency edges use the
    # authoritative cloud receipt even if a local copy was requested.
    runner._collect_job(key, job, files)
    return runner.CloudOutputs(job["delivery"], job["durable_job_id"], files)


def resume(identifier, volume_name=DEFAULT_VOLUME, output=None, timeout=0, *, volume=None):
    """Use only a cloud locator plus local SDK credentials to resume the DAG."""
    if timeout < 0:
        raise ValueError("Workflow timeout must be >= 0")
    volume = volume if volume is not None else _volume(volume_name)
    plan = w.load_plan(volume, volume_name, identifier)
    out = Path(output).resolve() if output else Path("outputs", "workflow-" + identifier).resolve()
    locator = {"workflow_id": identifier, "volume": volume_name}
    with runner._task_lock(out):
        marker = out / "workflow-run.json"
        if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) != locator:
            raise RuntimeError("Output directory belongs to another workflow")
        jobs = _build_run_jobs(plan, identifier, out)
        # Never adopt local status as execution evidence or overwrite another
        # workflow's local results. Missing status is entirely normal.
        for key, job in jobs.items():
            if job["status_file"].exists():
                saved = json.loads(job["status_file"].read_text(encoding="utf-8"))
                if (saved.get("intent_hash") != job["intent_hash"]
                        or saved.get("durable_job_id") != job["durable_job_id"]):
                    raise RuntimeError(f"Output directory has a different job identity: {key}")
        runner._save_json(marker, locator)
        deadline = runner._DEADLINE.set(time.monotonic() + timeout if timeout else None)
        try:
            completed = {sid: {} for sid, _ in jobs}
            runner._schedule_jobs(
                jobs, completed, plan["mode"],
                lambda key, job, done: _submit_planned(key, job, done, plan, identifier, volume),
                collect_job=_collect_planned)
        finally:
            runner._DEADLINE.reset(deadline)
    return out


def status(identifier, volume_name=DEFAULT_VOLUME, *, volume=None):
    volume = volume if volume is not None else _volume(volume_name)
    return w.inspect_plan(volume, w.load_plan(volume, volume_name, identifier), identifier)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Persist ALL inputs and an immutable workflow plan; no GPU")
    prep.add_argument("--task", required=True)
    prep.add_argument("--endpoints", default="endpoints.json")
    for name in ("status", "resume"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--run", required=True, help="Workflow UUID printed by prepare")
        cmd.add_argument("--volume", default=DEFAULT_VOLUME)
        if name == "resume":
            cmd.add_argument("--out")
            cmd.add_argument("--timeout", type=int, default=0)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.task, args.endpoints)
    elif args.command == "status":
        result = status(args.run, args.volume)
    else:
        result = {"workflow_id": args.run, "volume": args.volume,
                  "output": str(resume(args.run, args.volume, args.out, args.timeout))}
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
