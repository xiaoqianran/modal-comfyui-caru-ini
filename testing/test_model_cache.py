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

    def test_downloader_uses_scoped_volume_without_path_translation(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn('sub_path="/comfyui"', source)
        self.assertIn('benchmark_base = f"{CACHE_DIR}/.benchmark-downloads/{hf_id}"', source)
        self.assertIn('"benchmark_base": benchmark_base', source)
        self.assertNotIn('publisher_benchmark_base(', source)

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

    def _benchmark_evidence(self, base, items):
        """Fixture matching modal-benchmark v0.4.9 Coordinator receipts."""
        for item in items:
            repo = base / item["repo_id"] / item["revision"]
            source = self.ns["benchmark_path"](base, item)
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(self.content)
            receipt = repo / "_verification" / (
                hashlib.sha256(item["filename"].encode()).hexdigest() + ".json")
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(json.dumps({
                "version": 1, "repo_id": item["repo_id"],
                "revision": item["revision"], "filename": item["filename"],
                "size": item["size"],
                "source_sha256": item["digest"] if item["algorithm"] == "sha256" else None,
                "method": "trusted_xet_download",
                "mtime_ns": source.stat().st_mtime_ns,
                "xet_hash": "a" * 64, "huggingface_hub": "1.33.0", "hf_xet": "1.8.0",
            }))
        manifest = self.ns["benchmark_manifest"](items)
        (base / "download-manifest.json").write_text(json.dumps(manifest))
        first = manifest["models"][0]
        marker = base / first["repo_id"] / first["revision"] / "all-components-complete.json"
        marker.write_text(json.dumps({
            "status": "COMPLETE",
            "manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            "verification_mode": "xet",
        }))
        return marker

    def test_benchmark_manifest_and_publish(self):
        item = dict(kind='hf', repo_id='org/model', filename='nested/model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        manifest = self.ns['benchmark_manifest']([item, self.item])
        self.assertEqual(manifest['models'][0]['files'][item['filename']]['sha256'], item['digest'])
        base = self.cache / '.benchmark-downloads' / 'run'
        source = self.ns['benchmark_path'](base, item)
        self._benchmark_evidence(base, [item])
        payload = dict(items=[item], downloaded_items=[item], workflow='test', run_id='run',
                       benchmark_base=str(base))
        self.assertTrue(self.ns['merge_shards'](payload)['ok'])
        self.assertEqual(self.ns['_cached_path'](self.cache, item).read_bytes(), self.content)
        self.assertFalse(base.exists(), "benchmark staging must be removed after publish")

    def test_preexisting_blob_does_not_leave_second_weight_copy(self):
        item = dict(kind='hf', repo_id='org/model', filename='weights.safetensors',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'already-present'
        self._benchmark_evidence(base, [item])
        blob = self.cache / 'models--org--model' / 'blobs' / item['digest']
        blob.parent.mkdir(parents=True)
        blob.write_bytes(self.content)
        payload = dict(items=[item], downloaded_items=[item], workflow='test',
                       run_id='run', benchmark_base=str(base))
        self.assertTrue(self.ns['merge_shards'](payload)['ok'])
        self.assertEqual(blob.read_bytes(), self.content)
        self.assertFalse(base.exists(), 'duplicate benchmark weight must be cleaned')
        self.assertEqual(self.ns['_cached_path'](self.cache, item).read_bytes(), self.content)

    def test_benchmark_invalid_receipt_is_not_published(self):
        item = dict(kind='hf', repo_id='org/model', filename='model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'run'
        marker = self._benchmark_evidence(base, [item])
        payload = dict(items=[item], downloaded_items=[item], workflow='test', run_id='run',
                       benchmark_base=str(base))
        marker.write_text('{"status":"INCOMPLETE"}')
        with self.assertRaises(RuntimeError):
            self.ns['merge_shards'](payload)
        self.assertFalse(self.ns['_cached_path'](self.cache, item).exists())

    def test_benchmark_missing_receipt_is_not_published(self):
        item = dict(kind='hf', repo_id='org/model', filename='model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'run'
        self._benchmark_evidence(base, [item])
        receipt = base / item['repo_id'] / item['revision'] / '_verification' / (
            hashlib.sha256(item['filename'].encode()).hexdigest() + '.json')
        receipt.unlink()
        payload = dict(items=[item], downloaded_items=[item], workflow='test', run_id='run',
                       benchmark_base=str(base))
        with self.assertRaises(RuntimeError):
            self.ns['merge_shards'](payload)
        self.assertFalse(self.ns['_cached_path'](self.cache, item).exists())

    def test_benchmark_xet_receipt_tolerates_volume_mtime_drift(self):
        item = dict(kind='hf', repo_id='org/model', filename='model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'run'
        self._benchmark_evidence(base, [item])
        source = self.ns['benchmark_path'](base, item)
        stat = source.stat()
        os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
        payload = dict(items=[item], downloaded_items=[item], workflow='test', run_id='run',
                       benchmark_base=str(base))
        self.assertTrue(self.ns['merge_shards'](payload)['ok'])
        self.assertEqual(self.ns['_cached_path'](self.cache, item).read_bytes(), self.content)

    def test_benchmark_http_receipt_tolerates_mtime_drift(self):
        item = dict(kind='hf', repo_id='org/model', filename='config.json',
                    revision='a' * 40, size=len(self.content), algorithm='sha1',
                    digest=hashlib.sha1(f'blob {len(self.content)}'.encode()
                                        + bytes([0]) + self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'http'
        self._benchmark_evidence(base, [item])
        receipt = base / item['repo_id'] / item['revision'] / '_verification' / (
            hashlib.sha256(item['filename'].encode()).hexdigest() + '.json')
        record = json.loads(receipt.read_text())
        record.pop('xet_hash')
        record.pop('hf_xet')
        record.update(method='trusted_hf_http_download', etag=item['digest'])
        receipt.write_text(json.dumps(record))
        source = self.ns['benchmark_path'](base, item)
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        payload = dict(items=[item], downloaded_items=[item], workflow='test',
                       run_id='run', benchmark_base=str(base))
        self.assertTrue(self.ns['merge_shards'](payload)['ok'])

    def test_benchmark_changed_manifest_blocks_publication(self):
        item = dict(kind='hf', repo_id='org/model', filename='model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'run'
        self._benchmark_evidence(base, [item])
        (base / 'download-manifest.json').write_text('{"version":1,"models":[]}')
        payload = dict(items=[item], downloaded_items=[item], workflow='test',
                       run_id='run', benchmark_base=str(base))
        with self.assertRaisesRegex(RuntimeError, 'manifest'):
            self.ns['merge_shards'](payload)
        self.assertFalse(self.ns['_cached_path'](self.cache, item).exists())

    def test_benchmark_sources_and_verified_result(self):
        item = dict(kind='hf', repo_id='org/model', filename='nested/model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        items = [item, dict(item, filename='config.json')]
        sources = [{'repo_id': 'org/model', 'revision': 'a' * 40,
                    'include': 'nested/model.bin,config.json'}]
        self.assertEqual(self.ns['benchmark_sources'](items), sources)
        expected = self.ns['benchmark_manifest'](items)
        downloader = Mock()
        downloader.download.return_value = dict(
            status='VERIFIED', verification_mode='xet',
            base='/data/hf/.benchmark-downloads/one', manifest=expected)
        self.ns['download_hf_items'](
            items, '/data/hf/.benchmark-downloads/one', downloader, workers=3)
        downloader.download.assert_called_once_with(
            sources, base='/data/hf/.benchmark-downloads/one', workers=3)
        downloader.download.return_value['manifest'] = self.ns['benchmark_manifest']([item])
        with self.assertRaisesRegex(RuntimeError, 'manifest'):
            self.ns['download_hf_items'](
                items, '/data/hf/.benchmark-downloads/one', downloader, workers=3)

    def test_benchmark_sources_reject_unsafe_filters_and_revision_conflicts(self):
        item = dict(kind='hf', repo_id='org/model', filename='weight[1].bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        with self.assertRaises(ValueError):
            self.ns['benchmark_sources']([item])
        first = dict(item, filename='first.bin')
        second = dict(item, filename='second.bin', revision='b' * 40)
        with self.assertRaisesRegex(ValueError, 'revisions'):
            self.ns['benchmark_sources']([first, second])

    def test_benchmark_evidence_never_rehashes_weights(self):
        item = dict(kind='hf', repo_id='org/model', filename='model.bin',
                    revision='a' * 40, size=len(self.content), algorithm='sha256',
                    digest=hashlib.sha256(self.content).hexdigest())
        base = self.cache / '.benchmark-downloads' / 'run'
        self._benchmark_evidence(base, [item])
        payload = dict(items=[item], downloaded_items=[item], workflow='test', run_id='run',
                       benchmark_base=str(base))
        with patch.dict(self.ns, {'_verify_content': Mock(side_effect=AssertionError('unwanted hash'))}):
            self.assertTrue(self.ns['merge_shards'](payload)['ok'])

    def test_publish_failure_preserves_source_and_destination(self):
        target = self.cache / 'model.bin'
        target.write_bytes(b'old')
        with patch.object(os, 'replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                self.ns['_publish'](self.source, self.cache, self.item)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertEqual(self.source.read_bytes(), self.content)

    def test_first_commit_failure_keeps_published_data_and_old_record(self):
        record = self.cache / '.workflow-revisions/test.json'
        record.parent.mkdir()
        record.write_text('{"old":"commit"}')
        self.volume.commit.side_effect = OSError('commit failed')
        with self.assertRaises(OSError):
            self.ns['merge_shards'](self.payload)
        self.assertEqual((self.cache / 'model.bin').read_bytes(), self.content)
        self.assertEqual(json.loads(record.read_text()), {'old': 'commit'})

    def test_second_commit_failure_keeps_published_data(self):
        self.volume.commit.side_effect = [None, OSError('record commit failed')]
        with self.assertRaises(OSError):
            self.ns['merge_shards'](self.payload)
        self.assertEqual((self.cache / 'model.bin').read_bytes(), self.content)

    def test_success_commits_before_cleanup(self):
        def commit():
            self.assertEqual((self.cache / 'model.bin').read_bytes(), self.content)
        self.volume.commit.side_effect = commit
        self.assertTrue(self.ns['merge_shards'](self.payload)['ok'])
        self.assertEqual(self.volume.commit.call_count, 2)
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
            first = self.ns['fetch_manifest'](config, 'test')
            self.assertEqual(api.model_info.call_args.kwargs['revision'], 'c'*40)
            self.assertEqual(first['missing'], first['items'])
            self.assertFalse(first['published'])

            cached = self.ns['_cached_path'](self.cache, first['items'][0])
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b'valid')
            success = self.cache / '.workflow-revisions/test.json'
            success.parent.mkdir()
            success.write_text(pins.read_text())
            self.volume.commit.reset_mock()
            cached_plan = self.ns['fetch_manifest'](config, 'test')
            self.assertFalse(cached_plan['missing'])
            # Old revisions-only record must migrate without redownloading.
            self.assertFalse(cached_plan['published'])
            self.assertEqual(self.ns['quick_check'](config, 'test'), ['org/repo/weight'])
            self.volume.commit.assert_not_called()
            self.assertTrue(self.ns['merge_shards']({
                'items': cached_plan['items'], 'run_id': 'migrate-receipt',
                'workflow': 'test', 'downloaded_items': [],
            })['ok'])
            self.assertEqual(self.ns['quick_check'](config, 'test'), [])
            self.assertTrue(self.ns['fetch_manifest'](config, 'test')['published'])
            cached.write_bytes(b'bad')
            self.assertEqual(self.ns['quick_check'](config, 'test'), ['org/repo/weight'])
            cached.write_bytes(b'valid')
            self.ns['fetch_manifest'](config, 'test', True)
            self.assertEqual(api.model_info.call_args.kwargs['revision'], 'main')

    def test_external_shard_rejects_hf_without_network_or_volume_writes(self):
        item = dict(self.item, kind='hf', repo_id='org/repo', filename='weight', revision='a'*40)
        with self.assertRaisesRegex(ValueError, 'modal-benchmark'):
            self.ns['download_shard'](dict(items=[self.item, item], run_id='run', shard_id=0))
        self.volume.reload.assert_not_called()
        self.volume.commit.assert_not_called()

    def test_external_hf_url_keeps_local_filename(self):
        hf = types.ModuleType('huggingface_hub')
        item = dict(self.item, url='https://huggingface.co/org/model/resolve/main/nested/weight.bin')
        def download(**kwargs):
            downloaded = Path(kwargs['local_dir']) / kwargs['filename']
            downloaded.parent.mkdir(parents=True, exist_ok=True)
            downloaded.write_bytes(self.content)
            return str(downloaded)
        hf.hf_hub_download = Mock(side_effect=download)
        with patch.dict(sys.modules, huggingface_hub=hf):
            result = self.ns['download_shard'](dict(items=[item], run_id='run', shard_id=0))
        self.assertTrue(result['ok'], result)
        self.assertEqual(hf.hf_hub_download.call_args.kwargs['filename'], 'nested/weight.bin')
        target = self.ns['_cached_path'](self.ns['_stage_dir'](self.run_root, item), item)
        self.assertEqual(target.read_bytes(), self.content)

    def test_hf_worker_config(self):
        for value, expected in ((None, 3), ('', 3), ('1', 1), ('2', 2), ('8', 8)):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ['MODEL_HF_DOWNLOAD_WORKERS'] = value
                os.environ['MODEL_DOWNLOAD_WORKERS'] = '6'
                self.assertEqual(self.ns['hf_download_workers'](), expected)
        for value in ('0', '9', '1.5', 'abc'):
            with self.subTest(value=value), patch.dict(os.environ, MODEL_HF_DOWNLOAD_WORKERS=value):
                with self.assertRaisesRegex(ValueError, 'MODEL_HF_DOWNLOAD_WORKERS'):
                    self.ns['hf_download_workers']()

    def test_worker_config_reaches_benchmark_factory_and_download(self):
        item = dict(self.item, kind='hf', repo_id='org/model', filename='weight',
                    revision='a'*40)
        base = '/data/hf/.benchmark-downloads/one'
        downloader = Mock()
        downloader.download.return_value = dict(status='VERIFIED', verification_mode='xet',
            base=base, manifest=self.ns['benchmark_manifest']([item]))
        factory = Mock(return_value=downloader)
        module = types.ModuleType('modal_benchmark.hf')
        module.create_downloader = factory
        self.ns.update(MODEL_VOLUME_NAME='model-store', hf_secret_name='hf-secret')
        with patch.dict(sys.modules, {'modal_benchmark.hf': module}), patch.dict(
                os.environ, MODEL_HF_DOWNLOAD_WORKERS='8'):
            workers = self.ns['hf_download_workers']()
            self.ns['download_hf_items']([item], base, self.ns['create_hf_downloader'](workers),
                                         workers=workers)
        factory.assert_called_once_with(volume_name='model-store', volume_version=2,
            mount_path='/data', sub_path='/comfyui', secret_name='hf-secret',
            verification_mode='xet', max_workers=8)
        downloader.download.assert_called_once_with(
            self.ns['benchmark_sources']([item]), base=base, workers=8)

    def test_installed_benchmark_v049_persistent_config(self):
        from importlib.metadata import version
        self.assertEqual(version("modal-benchmark"), "0.4.9")
        self.ns.update(MODEL_VOLUME_NAME="model-store", hf_secret_name="hf-secret")
        for workers in (1, 3, 8):
            with self.subTest(workers=workers):
                config = self.ns["create_hf_downloader"](workers).config
                self.assertEqual(config.scheduling, "persistent")
                self.assertEqual(config.task_batch_bytes, 10 * 1024**3)
                self.assertEqual(config.commit_batch_files, 16)
                self.assertEqual(config.commit_batch_bytes, 16 * 1024**3)
                self.assertEqual(config.small_file_workers, 4)
                self.assertTrue(config.coalesce_commits)
                self.assertEqual(config.xet_concurrency, 8)
                self.assertEqual(config.replacement_budget, 0)
                self.assertEqual(config.volume_version, 2)
                self.assertEqual(config.mount_path, "/data")
                self.assertEqual(config.sub_path, "/comfyui")
                self.assertEqual(config.verification_mode, "xet")
                self.assertEqual(config.max_workers, workers)

    def test_installed_benchmark_receipts_match_publication(self):
        from modal_benchmark.hf.download import completion_identity
        from modal_benchmark.hf.evidence import publish_receipt
        from modal_benchmark.hf.transport import HUB_VERSION, XET_VERSION
        item = dict(self.item, kind="hf", repo_id="org/model", filename="nested/weight",
                    revision="a" * 40)
        base = self.cache / ".benchmark-downloads" / "real-contract"
        manifest = self.ns["benchmark_manifest"]([item])
        model = manifest["models"][0]
        marker = self._benchmark_evidence(base, [item])
        marker.write_text(json.dumps({**completion_identity(manifest), "verification_mode": "xet"}))
        source = self.ns["benchmark_path"](base, item)
        folder = base / item["repo_id"] / item["revision"]
        for transport in (
            {"method": "trusted_xet_download", "xet_hash": "a" * 64,
             "huggingface_hub": HUB_VERSION, "hf_xet": XET_VERSION},
            {"method": "trusted_hf_http_download", "etag": item["digest"],
             "huggingface_hub": HUB_VERSION},
            None,
        ):
            with self.subTest(transport=transport):
                publish_receipt(folder, model, item["filename"], source.stat(), transport=transport)
                self.ns["_require_benchmark_evidence"](base, [item])

    def test_invalid_http_etag_and_size_only_receipts_are_rejected(self):
        from modal_benchmark.hf.evidence import receipt_path, publish_receipt
        item = dict(self.item, kind="hf", repo_id="org/model", filename="weight",
                    revision="a" * 40, algorithm="sha1", digest="b" * 40)
        base = self.cache / ".benchmark-downloads" / "invalid-contract"
        self._benchmark_evidence(base, [item])
        model = self.ns["benchmark_manifest"]([item])["models"][0]
        source = self.ns["benchmark_path"](base, item)
        folder = base / item["repo_id"] / item["revision"]
        publish_receipt(folder, model, item["filename"], source.stat())
        with self.assertRaises(RuntimeError):
            self.ns["_require_benchmark_evidence"](base, [item])
        receipt = receipt_path(folder, item["filename"])
        record = json.loads(receipt.read_text())
        for invalid in ("z" * 40, "a" * 41):
            with self.subTest(etag=invalid):
                record.update(method="trusted_hf_http_download",
                              etag=invalid, huggingface_hub="1.33.0")
                receipt.write_text(json.dumps(record))
                with self.assertRaises(RuntimeError):
                    self.ns["_require_benchmark_evidence"](base, [item])

    def test_path_escape_rejected(self):
        for name in ('../bad', '/absolute', 'a/../../bad', 'a\\bad'):
            with self.assertRaises(ValueError):
                self.ns['_safe_path'](self.cache, name)


if __name__ == '__main__':
    unittest.main()
