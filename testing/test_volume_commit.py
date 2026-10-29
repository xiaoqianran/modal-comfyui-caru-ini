"""Unit checks for the periodic output Volume commit loop (no Modal/GPU needed)."""
import ast
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


class CommitTests(unittest.TestCase):
    def test_stable_outputs_commit_once_and_retry_after_error(self):
        source = Path(__file__).resolve().parents[1] / "comfyui.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComfyUI")
        methods = [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name in {"_output_snapshot", "_commit_outputs_loop"}]
        for node in methods:
            node.decorator_list = []
        namespace = {"Path": Path}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ClassDef(
            name="CommitHelper", bases=[], keywords=[], body=methods, decorator_list=[])],
            type_ignores=[])), str(source), "exec"), namespace)
        helper = namespace["CommitHelper"]()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "demo" / "video"
            output.mkdir(parents=True)
            item = output / "demo.mp4"
            item.write_bytes(b"video")
            namespace["WORKFLOW"] = "demo"
            namespace["output_volume"] = Mock()
            # The production snapshot references /outputs; redirect that root to test data.
            real_path = Path
            namespace["Path"] = lambda text: Path(tmp) if text == "/outputs" else real_path(text)
            snapshots = iter([{}, {str(item): (5, 1)}, {str(item): (5, 1)},
                              {str(item): (5, 1)}, {str(item): (6, 2)},
                              {str(item): (6, 2)}])
            helper._output_snapshot = lambda: next(snapshots)
            stop = Mock()
            stop.wait.side_effect = [False] * 6 + [True]
            helper.commit_stop = stop
            namespace["output_volume"].commit.side_effect = [RuntimeError("temporary"), None, None]
            helper._commit_outputs_loop()
            self.assertEqual(namespace["output_volume"].commit.call_count, 3)

    def test_snapshot_skips_temporary_and_empty_files(self):
        source = Path(__file__).resolve().parents[1] / "comfyui.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ComfyUI")
        fn = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_output_snapshot")
        fn.decorator_list = []
        namespace = {"Path": Path}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), str(source), "exec"), namespace)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "demo" / "video"
            d.mkdir(parents=True)
            (d / "ok.mp4").write_bytes(b"ok")
            (d / "temp.mp4.part").write_bytes(b"partial")
            (d / "zero.mp4").touch()
            namespace["WORKFLOW"] = "demo"
            namespace["Path"] = lambda name: Path(tmp) if name == "/outputs" else Path(name)
            result = namespace["_output_snapshot"]()
            self.assertEqual(list(result), [str(d / "ok.mp4")])


if __name__ == "__main__":
    unittest.main()
