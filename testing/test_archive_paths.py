"""Offline regression: single/dual ComfyUI outputs and frontend discovery."""
import tempfile
import ast
from pathlib import Path
from unittest.mock import patch

from storage import hf_sync, s3
from frontend import server


def test_single_and_dual_archive():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "outputs"
        preview = Path(tmp) / "previews"
        paths = {
            "image/Qwen_image_00001.png": root / "Qwen_image_00001.png",
            "video/legacy.mp4": root / "video" / "legacy.mp4",
            "video/instance-1/clip.mp4": root / "instance-1" / "video" / "clip.mp4",
            "video/instance-2/clip.mp4": root / "instance-2" / "video" / "clip.mp4",
            "image/instance-2/pic.png": root / "instance-2" / "pic.png",
            "3d/instance-2/mesh.glb": root / "instance-2" / "3d" / "mesh.glb",
        }
        for relative, path in paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
        (root / "instance-1" / "readme.json").write_text("{}")
        class FakeHF:
            def __init__(self):
                self.objects = {}
                self.uploads = []
            def upload_file(self, path, bucket, key, Config=None, ExtraArgs=None):
                self.objects[key] = (Path(path).stat().st_size, ExtraArgs["Metadata"]["sha256"])
                self.uploads.append(key)
        hf = FakeHF()
        def same_content(client, bucket, key, size, digest):
            return client.objects.get(key) == (size, digest)
        with patch.object(hf_sync, "ROOT", root), \
             patch.object(hf_sync, "PREVIEWS", preview), \
             patch.object(hf_sync, "WORKFLOW", "test-suite"), \
             patch.object(hf_sync, "INTERVAL", 2), \
             patch.object(hf_sync, "same_content", same_content), \
             patch.object(hf_sync, "make_video_preview", lambda _: None):
            assert dict(hf_sync.iter_outputs()) == paths
            observed, completed = {}, {}
            hf_sync.archive_once(hf, observed, completed, now=0)
            assert not hf.uploads, "Unstable files must not be archived yet"
            hf_sync.archive_once(hf, observed, completed, now=3)
            assert len(hf.uploads) == len(paths)
            for relative in paths:
                key = f"comfyui/test-suite/{relative}"
                assert key in hf.objects
                assert s3.check_key(key, "hf") == key
                if relative.endswith(".png"):
                    assert (preview / relative).read_bytes() == paths[relative].read_bytes()
                    assert s3.check_key(f"previews/test-suite/{relative}", "r2")
            hf_sync.archive_once(hf, observed, completed, now=10)
            assert len(hf.uploads) == len(paths), "Stable files must not reupload"
            paths["image/instance-2/pic.png"].write_bytes(b"new larger image")
            hf_sync.archive_once(hf, observed, completed, now=11)
            hf_sync.archive_once(hf, observed, completed, now=15)
            assert len(hf.uploads) == len(paths) + 1, "Changed output must rearchive"
            paths["image/instance-2/pic.png"].write_bytes(b"new larger imagE")
            hf_sync.archive_once(hf, observed, completed, now=16)
            hf_sync.archive_once(hf, observed, completed, now=20)
            assert len(hf.uploads) == len(paths) + 2, "Same-size changed output must rearchive"


def test_modal_volume_snapshot_scans_all_instances():
    """Inspect the actual snapshot method without importing/deploying Modal."""
    source = (Path(__file__).resolve().parents[1] / "comfyui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    method = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_output_snapshot")
    method.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = [
            root / "test-suite" / "Qwen_001.png",
            root / "test-suite" / "instance-1" / "video" / "clip.mp4",
            root / "test-suite" / "instance-2" / "3d" / "mesh.glb",
        ]
        for path in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"123")
        (root / "test-suite" / "temp.part").write_bytes(b"ignore")
        def fake_path(name):
            return root if name == "/outputs" else Path(name)
        ns = {"Path": fake_path, "WORKFLOW": "test-suite"}
        exec(compile(module, "<snapshot>", "exec"), ns)
        snapshot = ns["_output_snapshot"]()
        assert set(snapshot) == {str(p) for p in files}


def test_frontend_nested_workflows_and_urls():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        folder = root / "workflows" / "comfyui-b300-image-video-suite"
        nested = folder / "qwen"
        nested.mkdir(parents=True)
        (folder / "models.json").write_text('{"gpu":"B300","models":[]}')
        (folder / "plugins.json").write_text('{"comfy_plugins":[]}')
        (nested / "image_t2i.json").write_text('{"1":{"class_type":"Example"}}')
        logs = root / "logs"
        logs.mkdir()
        (logs / "comfyui-b300-image-video-suite.log").write_text(
            "Deployment complete\nComfyUI.ui -> https://someone--comfyui-1.us-east.modal.run/\n")
        with patch.object(server, "ROOT", root), \
             patch.object(server, "LOG_DIR", logs), \
             patch.object(server, "LINKS", {}):
            flows = server.workflows()
            assert len(flows) == 1 and flows[0]["id"] == "comfyui-b300-image-video-suite"
            assert flows[0]["gpu"] == "B300"
            assert server.saved_links() == {
                "comfyui-b300-image-video-suite": "https://someone--comfyui-1.us-east.modal.run/"
            }


if __name__ == "__main__":
    test_single_and_dual_archive()
    test_frontend_nested_workflows_and_urls()
    test_modal_volume_snapshot_scans_all_instances()
    print("PASS archive paths: single/dual/image/3d, stable upload, frontend discovery")
