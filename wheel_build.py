"""Build workflow-local CUDA wheels on Modal and optionally publish GitHub Releases."""

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import modal

ROOT = Path(__file__).parent
REPOSITORY = "xiaoqianran/modal-comfyui-caru-ini"
CUDA_IMAGE = "nvidia/cuda:12.6.3-devel-ubuntu22.04"

app = modal.App("comfyui-wheel-builder")
image = (
    modal.Image.from_registry(CUDA_IMAGE, add_python="3.11")
    .apt_install("git", "build-essential", "ninja-build", "git-lfs")
    .pip_install("comfy-cli")
    .run_commands("comfy --skip-prompt install --nvidia")
    .pip_install("wheel", "setuptools")
    .add_local_dir(ROOT / "workflows", "/workspace/workflows", copy=True)
)


def github_request(url: str, token: str, data: bytes, method: str, content_type: str):
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
            "User-Agent": "modal-comfyui-wheel-builder",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read()) if "json" in response.headers.get("Content-Type", "") else None


def publish_release(tag: str, output: Path, token: str):
    base = f"https://api.github.com/repos/{REPOSITORY}/releases"
    metadata = (output / "BUILD-INFO.txt").read_text(encoding="utf-8")
    release = github_request(
        base, token,
        json.dumps({"tag_name": tag, "name": tag, "body": metadata, "draft": True}).encode(),
        "POST", "application/json",
    )
    try:
        upload_base = release["upload_url"].split("{", 1)[0]
        for artifact in sorted(output.iterdir()):
            if artifact.suffix != ".whl" and artifact.name not in {"SHA256SUMS", "BUILD-INFO.txt"}:
                continue
            upload_url = upload_base + "?name=" + urllib.parse.quote(artifact.name)
            github_request(upload_url, token, artifact.read_bytes(), "POST", "application/octet-stream")
        github_request(
            f"{base}/{release['id']}", token,
            json.dumps({"draft": False}).encode(), "PATCH", "application/json",
        )
    except Exception:
        print(f"Release upload incomplete; draft retained: {release['html_url']}")
        raise
    return release["html_url"]


@app.function(
    image=image,
    gpu="L40S",
    timeout=3600,
    secrets=[modal.Secret.from_name("github")],
)
def build(workflow: str, publish: bool = False, tag: str = ""):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", workflow):
        raise ValueError("Invalid workflow name")
    directory = Path("/workspace/workflows") / workflow
    script = directory / "build-wheels.sh"
    if not script.is_file() or not (directory / "plugins.json").is_file():
        raise FileNotFoundError(f"Workflow build script/config missing: {workflow}")
    if publish and (not tag or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", tag)):
        raise ValueError("Publishing requires a valid --tag")
    if publish and not os.getenv("GITHUB_TOKEN"):
        raise ValueError("Modal Secret 'github' must contain GITHUB_TOKEN")

    subprocess.run(["bash", str(script)], check=True, cwd="/workspace")
    output = directory / "wheels"
    artifacts = sorted(output.glob("*.whl"))
    if not artifacts:
        raise RuntimeError("Build produced no wheel")
    for artifact in artifacts:
        with zipfile.ZipFile(artifact) as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"Invalid wheel {artifact.name}: {bad}")
    print((output / "BUILD-INFO.txt").read_text(encoding="utf-8"))
    print((output / "SHA256SUMS").read_text(encoding="utf-8"))
    if not publish:
        print("Dry run: built and validated wheels; no GitHub release created.")
        return
    url = publish_release(tag, output, os.environ["GITHUB_TOKEN"])
    torch_version = next(
        line.split("=", 1)[1].strip()
        for line in (output / "BUILD-INFO.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("torch=")
    )
    print("Release:", url)
    print("Copy this into the corresponding plugin in plugins.json:")
    print(json.dumps({
        "wheel_torch": torch_version,
        "wheels": [
            {
                "url": f"https://github.com/{REPOSITORY}/releases/download/{tag}/{f.name}",
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            }
            for f in artifacts
        ],
    }, indent=2))


@app.local_entrypoint()
def main(workflow: str = "community-hunyuan3d-2-1-visualbruno", publish: bool = False, tag: str = ""):
    build.remote(workflow, publish, tag)
