"""Offline downloader fault tests; no Modal imports, network or credentials."""
import ast
import hashlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'download-models.py'


def load_functions():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for node in functions:
        node.decorator_list = []
    ns = dict(Path=Path, json=json, os=os, WORKFLOW='test')
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), 'exec'), ns)
    return ns


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.ns = load_functions()
        self.volume = Mock()
        self.ns.update(CACHE_DIR=str(self.cache), SHARD_ROOT=str(self.cache / '.download-shards'), volume=self.volume)
        self.content = b'valid model bytes'
        self.item = dict(kind='external', filename='model.bin', url='https://example.test/model', size=len(self.content),
                         algorithm='sha256', digest=hashlib.sha256(self.content).hexdigest())
        self.run_root = Path(self.ns['SHARD_ROOT']) / 'run'
        self.stage = self.ns['_stage_dir'](self.run_root, self.item)
        self.source = self.stage / self.item['filename']
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(self.content)
        self.payload = dict(items=[self.item], downloaded_items=[self.item], workflow='test', run_id='run')

    def test_cheap_hit_does_not_hash_but_full_check_detects_corruption(self):
        self.source.write_bytes(b'x' * len(self.content))
        self.assertTrue(self.ns['_size_valid'](self.source, self.item))
        with self.assertRaises(ValueError):
            self.ns['_verify_content'](self.source, self.item)

    def test_permission_error_is_not_cache_miss(self):
        with patch.object(Path, 'stat', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.ns['_size_valid'](self.source, self.item)

    def test_git_blob_hash(self):
        item = dict(self.item, algorithm='sha1', digest=hashlib.sha1(f'blob {len(self.content)}\0'.encode() + self.content).hexdigest())
        self.ns['_verify_content'](self.source, item)

    def test_publish_failure_preserves_source_and_destination(self):
        target = self.cache / 'model.bin'
        target.write_bytes(b'old')
        with patch.object(os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.ns['_publish'](self.source, self.cache, self.item, True)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_first_commit_failure_keeps_source_and_old_record(self):
        record = self.cache / '.workflow-revisions/test.json'
        record.parent.mkdir()
        record.write_text('{"old":"commit"}')
        self.volume.commit.side_effect = OSError('commit failed')
        with self.assertRaises(OSError):
            self.ns['merge_shards'](self.payload)
        self.assertTrue(self.source.exists())
        self.assertEqual(json.loads(record.read_text()), {'old': 'commit'})

    def test_second_commit_failure_keeps_source(self):
        self.volume.commit.side_effect = [None, OSError('record commit failed')]
        with self.assertRaises(OSError):
            self.ns['merge_shards'](self.payload)
        self.assertTrue(self.source.exists())

    def test_success_commits_before_cleanup(self):
        def commit():
            if self.volume.commit.call_count <= 2:
                self.assertTrue(self.source.exists())
        self.volume.commit.side_effect = commit
        self.assertTrue(self.ns['merge_shards'](self.payload)['ok'])
        self.assertEqual(self.volume.commit.call_count, 3)
        self.assertFalse(self.run_root.exists())
        self.assertEqual((self.cache / 'model.bin').read_bytes(), self.content)

    def test_staging_independent_of_shard_assignment(self):
        shuffled = dict(reversed(list(self.item.items())))
        self.assertEqual(self.stage, self.ns['_stage_dir'](self.run_root, shuffled))

    def test_shard_reuses_verified_stage_without_network(self):
        hf = types.ModuleType('huggingface_hub')
        hf.hf_hub_download = Mock(side_effect=AssertionError('unexpected download'))
        with patch.dict(sys.modules, huggingface_hub=hf):
            result = self.ns['download_shard'](dict(items=[self.item], run_id='run', shard_id=8, workers=2))
        self.assertTrue(result['ok'], result)
        self.assertFalse((self.cache / 'model.bin').exists())

    def test_manifest_uses_pins_until_explicit_update(self):
        hf = types.ModuleType('huggingface_hub')
        entry = types.SimpleNamespace(rfilename='weight', size=5, lfs=types.SimpleNamespace(sha256='a'*64))
        api = Mock()
        api.model_info.return_value = types.SimpleNamespace(sha='b'*40, siblings=[entry])
        hf.HfApi = Mock(return_value=api)
        pins = self.cache / '.workflow-pins/test.json'
        pins.parent.mkdir()
        pins.write_text(json.dumps({'org/repo/weight': 'c'*40}))
        config = json.dumps(dict(models=[dict(repo_id='org/repo', filename='weight')]))
        with patch.dict(sys.modules, huggingface_hub=hf):
            self.ns['fetch_manifest'](config, 'test')
            self.assertEqual(api.model_info.call_args.kwargs['revision'], 'c'*40)
            self.ns['fetch_manifest'](config, 'test', True)
            self.assertEqual(api.model_info.call_args.kwargs['revision'], 'main')

    def test_hf_native_resume_and_corrupt_cache_recovery(self):
        hf = types.ModuleType('huggingface_hub')
        item = dict(self.item, kind='hf', repo_id='org/repo', filename='weight', revision='a'*40)
        target = self.ns['_cached_path'](self.ns['_stage_dir'](self.run_root, item), item)
        def download(**kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.content)
            return str(target)
        hf.hf_hub_download = Mock(side_effect=download)
        payload = dict(items=[item], run_id='run', shard_id=0)
        with patch.dict(sys.modules, huggingface_hub=hf):
            result = self.ns['download_shard'](payload)
            self.assertTrue(result['ok'], result)
            self.assertFalse(hf.hf_hub_download.call_args.kwargs['force_download'])
            target.write_bytes(b'x' * len(self.content))
            result = self.ns['download_shard'](payload)
            self.assertTrue(result['ok'], result)
            self.assertTrue(hf.hf_hub_download.call_args.kwargs['force_download'])
        self.assertFalse(self.ns['_cached_path'](self.cache, item).exists())

    def test_bad_download_never_publishes(self):
        hf = types.ModuleType('huggingface_hub')
        item = dict(self.item, kind='hf', repo_id='org/repo', filename='weight', revision='a'*40)
        target = self.ns['_cached_path'](self.ns['_stage_dir'](self.run_root, item), item)
        def download(**kwargs):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'x' * len(self.content))
            return str(target)
        hf.hf_hub_download = Mock(side_effect=download)
        with patch.dict(sys.modules, huggingface_hub=hf):
            result = self.ns['download_shard'](dict(items=[item], run_id='run', shard_id=0))
        self.assertFalse(result['ok'])
        self.assertEqual(hf.hf_hub_download.call_count, 2)
        self.assertTrue(target.exists())
        self.assertFalse(self.ns['_cached_path'](self.cache, item).exists())

    def test_path_escape_rejected(self):
        for name in ('../bad', '/absolute', 'a/../../bad', 'a\\bad'):
            with self.assertRaises(ValueError):
                self.ns['_safe_path'](self.cache, name)


if __name__ == '__main__':
    unittest.main()
