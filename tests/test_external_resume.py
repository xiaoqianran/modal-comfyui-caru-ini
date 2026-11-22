import ast, json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_download_model_publish import SOURCE


class ResumeTest(unittest.TestCase):
    def test_range_accepted_or_ignored(self):
        nodes = ast.parse(SOURCE.read_text(encoding="utf8")).body
        names = {"_download_external", "_verify_content", "_size_valid"}
        module = ast.Module([n for n in nodes if getattr(n, "name", "") in names], [])
        ctx = dict(json=json, os=os, Path=Path)
        exec(compile(module, str(SOURCE), "exec"), ctx)
        for status in (200, 206):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "model"
                url = "https://example.invalid/model"
                path.with_suffix(".part").write_bytes(b"ab")
                path.with_suffix(".part.resume").write_text(json.dumps(
                    dict(url=url, size=4, etag='"v1"')))
                http = MagicMock()
                response = http.stream.return_value.__enter__.return_value
                response.status_code = status
                response.url = url
                response.headers = {"Content-Range": "bytes 2-3/4", "ETag": '"v1"'}
                response.iter_bytes.return_value = [b"cd" if status == 206 else b"abcd"]
                with patch.dict(sys.modules, httpx=http):
                    ctx["_download_external"](dict(url=url, size=4), path)
                self.assertEqual(http.stream.call_args.kwargs["headers"]["Range"], "bytes=2-")
                self.assertEqual(path.read_bytes(), b"abcd")

class HubUrlTest(unittest.TestCase):
    def test_single_model_resolve_url(self):
        nodes = ast.parse(SOURCE.read_text(encoding="utf8")).body
        fn = next(n for n in nodes if getattr(n, "name", "") == "_hf_model_file")
        ctx = {}
        exec(compile(ast.Module([fn], []), str(SOURCE), "exec"), ctx)
        parse = ctx["_hf_model_file"]
        self.assertEqual(parse("https://huggingface.co/org/model/resolve/main/sub/a%20b.bin?download=true"),
                         dict(repo_id="org/model", revision="main", filename="sub/a b.bin"))
        for url in ("https://example.org/org/model/resolve/main/a",
                    "https://huggingface.co/datasets/name/resolve/main/a",
                    "https://huggingface.co/org/model/resolve/main/%2e%2e/a"):
            self.assertIsNone(parse(url))
