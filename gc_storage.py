"""Preview or explicitly apply conservative offline durable-media garbage collection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from storage import gc
import batch_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Read-only dependency scan and deletion plan")
    plan.add_argument("--volume", required=True, help="Explicit Output Volume V2 name")
    plan.add_argument("--min-age-hours", type=float, default=gc.DEFAULT_MIN_AGE_HOURS)
    plan.add_argument("--out", required=True, help="Local JSON plan")
    apply = commands.add_parser("apply", help="Delete exact files from a fresh, unchanged plan")
    apply.add_argument("--volume", required=True)
    apply.add_argument("--plan", required=True)
    apply.add_argument("--confirm-quiescent", action="store_true",
                       help="Confirm ALL users/apps/mounts/writers are stopped for the entire GC operation")
    args = parser.parse_args()
    if args.command == "apply" and not args.confirm_quiescent:
        parser.error("apply requires --confirm-quiescent; this is offline GC")
    import modal
    volume = modal.Volume.from_name(args.volume, version=2)
    if args.command == "plan":
        result = gc.scan(volume, args.volume, min_age_hours=args.min_age_hours)
        batch_run._save_json(Path(args.out), result)
        print(json.dumps({k: v for k, v in result.items() if k != "candidates"} |
                         {"candidate_count": len(result["candidates"]), "plan": str(Path(args.out).resolve())},
                         ensure_ascii=False, indent=2))
    else:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        result = gc.apply(volume, args.volume, plan, confirm_quiescent=args.confirm_quiescent,
                          on_delete=lambda item: print(json.dumps({"deleted": item}, ensure_ascii=False), flush=True))
        print(json.dumps({"deleted_count": len(result["deleted"]), "deleted_bytes": result["deleted_bytes"]},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
