"""Inspect, reattach or download an existing durable job without local sources."""
import argparse
import json
import time
from pathlib import Path

from storage.execution_client import DurableClient
from storage.delivery import normalized_config


def download_result(client, result, out):
    if result["status"] != "completed":
        raise RuntimeError("Durable result is not completed")
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for index, item in enumerate(result["remote_files"], 1):
        # Use an ordinal to prevent collisions between equal output filenames.
        target = out / f"{index:03d}_{item['filename']}"
        evidence = client.download(result["receipt"], item, target)
        files.append({"path": str(target.resolve()), **evidence})
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "resume", "download", "retry"])
    parser.add_argument("--config", required=True, help="JSON file containing the step.delivery configuration")
    parser.add_argument("--job", required=True, help="Durable job UUID")
    parser.add_argument("--endpoint", default="", help="Native URL, required only for a new authenticated dispatch")
    parser.add_argument("--attempt", help="New retry identity (retry only)")
    parser.add_argument("--allow-unknown", action="store_true", help="Acknowledge that the old claimed attempt might still finish")
    parser.add_argument("--call-id", help="Reattach to an existing Modal call (resume only)")
    parser.add_argument("--out", type=Path, help="Directory for verified downloads")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    config = normalized_config(json.loads(Path(args.config).read_text(encoding="utf-8-sig")))
    client = DurableClient(config, args.endpoint)
    if args.action == "retry":
        if not args.attempt:
            parser.error("retry requires --attempt; it prepares only, then use resume")
        state = client.prepare_retry(args.job, args.attempt, args.allow_unknown)
    elif args.action == "resume":
        state = client.status(args.job)
        if state["status"] not in ("completed", "failed"):
            if args.call_id:
                state = {"status": "dispatched", "call_id": args.call_id}
            else:
                if not args.endpoint:
                    parser.error("Specify --endpoint to dispatch/reconcile an unfinished job")
                state = client.start(args.job)
            if state["status"] == "dispatched":
                call_id = state["call_id"]
                print(json.dumps({"job_id": args.job, "call_id": call_id}), flush=True)
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    state = client.poll(args.job, call_id)
                    if state is not None:
                        break
                    time.sleep(2)
                else:
                    raise TimeoutError("Observation timeout; keep the job ID and call ID to reattach")
    else:
        state = client.status(args.job)
    if args.action == "download":
        if not args.out:
            parser.error("download requires --out")
        print(json.dumps(download_result(client, state, args.out), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    if args.action == "resume" and state["status"] != "completed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
