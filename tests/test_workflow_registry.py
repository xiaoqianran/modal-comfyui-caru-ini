import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_scanner():
    module = ast.parse((ROOT / "comfyui.py").read_text(encoding="utf-8"))
    func = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == "find_cnr_ids")
    namespace = {}
    exec(compile(ast.Module(body=[func], type_ignores=[]), "<scanner>", "exec"), namespace)
    return namespace["find_cnr_ids"]


class WorkflowRegistryTest(unittest.TestCase):
    def test_nested_subgraph_ids_and_duplicate_removal(self):
        scan = load_scanner()
        workflow = {
            "nodes": [{"cnr_id": "comfy-core"}, {"cnr_id": "comfyui-sharp"}],
            "extra": {"subgraphs": [{"nodes": [
                {"cnr_id": "comfyui-sharp"}, {"cnr_id": "ComfyUI-GaussianPack"},
                {"cnr_id": None},
            ]}]},
        }
        self.assertEqual(
            scan(workflow) - {"comfy-core"},
            {"comfyui-sharp", "ComfyUI-GaussianPack"},
        )

    def test_existing_workflow_jsons_parse_and_scan(self):
        scan = load_scanner()
        count = 0
        for workflow in (ROOT / "workflows").iterdir():
            if not workflow.is_dir():
                continue
            for path in workflow.rglob("*.json"):
                if path.name.lower() in {"models.json", "plugins.json"}:
                    continue
                self.assertIsInstance(scan(json.loads(path.read_text(encoding="utf-8"))), set)
                count += 1
        self.assertGreater(count, 0)


if __name__ == "__main__":
    unittest.main()
