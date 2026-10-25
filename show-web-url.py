"""Print the existing deployed ComfyUI URL without changing any Web Function."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Show the actual Modal ComfyUI.ui Web URL")
    parser.add_argument("--workflow", default=os.getenv("WORKFLOW", "comfyui-pixal3d-trellis2"))
    parser.add_argument("--profile", help="Modal profile; defaults to the active profile")
    args = parser.parse_args()
    identifier = f"modal-{args.workflow}/ComfyUI.ui"
    command = ["modal", "function", "info", identifier, "--json"]
    if args.profile:
        command.extend(["--profile", args.profile])
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )
    if result.returncode:
        print(f"Unable to query URL for {identifier}:", file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("Unexpected Modal CLI response", file=sys.stderr)
        print(result.stdout[:1500], file=sys.stderr)
        return 1
    url = ((info.get("method_details") or {}).get("ui") or {}).get("web_url")
    url = url or (info.get("web_info") or {}).get("web_url")
    if not url:
        print(f"Modal did not return a Web URL for {identifier}", file=sys.stderr)
        return 1
    print(f"ComfyUI URL ({args.workflow}):")
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())